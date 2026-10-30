"""Bounded, read-only discovery of literal message routes in loaded plugins."""

import ast
import re
from dataclasses import dataclass, field, fields, replace

from .analysis_cache import AnalysisCache
from .registration import command_roots
from .source_index import (
    ClassValue,
    FunctionValue,
    InstanceValue,
    ModuleValue,
    RuntimeValue,
    SourceIndex,
)

MAX_STEPS = 80000
MAX_DEPTH = 8
MAX_ENTRIES = 256
MAX_BRANCHES = 128
UNKNOWN = object()
DENIED = object()
_RESULTS = AnalysisCache()


@dataclass(frozen=True)
class PublicEntry:
    command: str
    description: str
    usage: str = ""
    aliases: tuple[str, ...] = ()
    conditional: bool = False
    use_prefix: bool = True


@dataclass(frozen=True)
class DiscoveryResult:
    entries: tuple[PublicEntry, ...] = ()
    restricted_commands: tuple[str, ...] = ()
    context_allowed: bool = True
    incomplete: bool = False


@dataclass(frozen=True)
class Text:
    index: int = 0
    token: bool = False
    skip: int = 0
    slash_stripped: bool = False
    roots: tuple[str, ...] = ()


@dataclass(frozen=True)
class Parts:
    index: int
    tail: bool = False


@dataclass(frozen=True)
class Length:
    value: object


@dataclass(frozen=True)
class Operation:
    owner: object
    name: str


@dataclass(frozen=True)
class EventValue:
    pass


@dataclass(frozen=True)
class Access:
    value: object = UNKNOWN


@dataclass(frozen=True)
class RegexValue:
    pattern: str


@dataclass(frozen=True)
class MatchValue:
    start: int
    roots: tuple[str, ...]
    has_args: bool = False


@dataclass
class State:
    env: dict = field(default_factory=dict)
    route: dict = field(default_factory=dict)
    excluded: dict = field(default_factory=dict)
    conditional: bool = False
    permission_unknown: bool = False
    matched: bool = False
    description: str = ""
    usage: str = ""
    context_gate: bool = False
    denied: bool = False
    delegated: bool = False

    def copy(self):
        return replace(
            self,
            env=dict(self.env),
            route=dict(self.route),
            excluded={key: set(value) for key, value in self.excluded.items()},
        )


def _state_key(value, depth=0):
    """Compare analyzer values without traversing/calling live plugin objects."""
    kind = type(value)
    if depth > 16:
        return id(kind), id(value)
    if kind in (str, bytes, int, float, bool, type(None)):
        return kind, value
    if isinstance(value, RuntimeValue):
        return kind, id(value.value)
    if isinstance(value, ModuleValue):
        return kind, str(value.path), value.prefix
    if kind in (dict,):
        return kind, tuple(
            (_state_key(k, depth + 1), _state_key(v, depth + 1))
            for k, v in value.items()
        )
    if kind in (tuple, list):
        return kind, tuple(_state_key(item, depth + 1) for item in value)
    if kind in (set, frozenset):
        return kind, frozenset(_state_key(item, depth + 1) for item in value)
    if kind in (
        State,
        Text,
        Parts,
        Length,
        Operation,
        EventValue,
        Access,
        RegexValue,
        MatchValue,
        ClassValue,
        InstanceValue,
        FunctionValue,
    ):
        return kind, tuple(
            _state_key(getattr(value, item.name), depth + 1) for item in fields(value)
        )
    return id(kind), id(value)


def _access_name(value):
    value = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(value)).casefold()
    parts = set(re.findall(r"[a-z]+|[\u4e00-\u9fff]+", value))
    return bool(
        parts
        & {
            "admin",
            "admins",
            "administrator",
            "administrators",
            "superuser",
            "superusers",
            "owner",
            "owners",
            "permission",
            "permissions",
            "privilege",
            "privileged",
            "moderator",
            "moderators",
            "sudo",
            "管理员",
            "权限",
            "房主",
            "群主",
        }
    )


def _access_expr(node):
    for item in ast.walk(node):
        if isinstance(item, (ast.Name, ast.Attribute)):
            if _access_name(item.id if isinstance(item, ast.Name) else item.attr):
                return True
        if isinstance(item, ast.Subscript) and isinstance(item.slice, ast.Constant):
            if isinstance(item.slice.value, str) and _access_name(item.slice.value):
                return True
    return False


def _restricted_decorator(node):
    if not _access_expr(node):
        return False
    if isinstance(node, ast.Call):
        name = (
            node.func.attr
            if isinstance(node.func, ast.Attribute)
            else (node.func.id if isinstance(node.func, ast.Name) else "")
        )
        if name == "permission_type" and node.args:
            argument = node.args[0]
            if isinstance(argument, ast.Attribute) and argument.attr == "MEMBER":
                return False
    return True


def _summary(text):
    for line in str(text or "").splitlines():
        line = re.sub(r"[`*_#]", "", line).strip()
        if line and not re.match(r"(?i)^(args|returns|参数|用法)\s*[:：]", line):
            return line[:76] + ("…" if len(line) > 76 else "")
    return ""


def _strings(value):
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (tuple, list, set, frozenset, dict)):
        return tuple(item for item in value if isinstance(item, str))
    return ()


def _simple_regex(pattern):
    """Accept only finite command words followed by a proven token boundary."""
    if not isinstance(pattern, str) or len(pattern) > 512:
        return (), False
    pattern = re.sub(r"^\(\?[aiLmsux]+\)", "", pattern)
    if not pattern.startswith("^") or not pattern.endswith("$"):
        return (), False
    body = pattern[1:-1]
    tails = (
        (r"(?:\s+(.*))?", True),
        (r"(?:\s+(.+))?", True),
        (r"(?:\s+.*)?", True),
        (r"\s+(.+)", True),
        (r"\s+(.*)", True),
        (r"(?:\s+.+)?", True),
        (r"\s*", False),
    )
    args = False
    for suffix, has_args in tails:
        if body.endswith(suffix):
            body, args = body[: -len(suffix)], has_args
            break
    optional_prefixes = ("",)
    for marker, prefixes in (
        ("[/!] ?".replace(" ", ""), ("", "/", "!")),
        ("[/]?", ("", "/")),
        ("/?", ("", "/")),
    ):
        if marker in body:
            body = body.replace(marker, "", 1)
            optional_prefixes = prefixes
            break
    if body.startswith("(?:") and body.endswith(")"):
        body = body[3:-1]
    elif body.startswith("(") and body.endswith(")"):
        body = body[1:-1]
    literal_prefix = ""
    if body.startswith("/(?:") and body.endswith(")"):
        literal_prefix, body = "/", body[4:-1]
    elif body.startswith("/(") and body.endswith(")"):
        literal_prefix, body = "/", body[2:-1]
    words = body.split("|")
    if not words or len(words) > 12:
        return (), False
    roots = []
    for word in words:
        word = literal_prefix + word.replace(r"\/", "/").replace(r"\!", "!")
        if not word or not re.fullmatch(r"[/!\w\u4e00-\u9fff-]+", word):
            return (), False
        roots.extend(prefix + word for prefix in optional_prefixes)
    return tuple(dict.fromkeys(roots)), args


class Scanner:
    def __init__(self, plugin, handler, event=None, snapshot=None):
        self.index = SourceIndex(plugin, handler)
        self.plugin = plugin
        self.handler = handler
        self.event = event
        self.entries = {}
        self.restricted = set()
        self.steps = 0
        self.stack = []
        self.incomplete = False
        self.context_rejected = False
        self.permission_rejected = False
        self.started = False
        self.snapshot = {}
        self._access_cache = {}
        if snapshot is not None:
            self.snapshot = snapshot
        elif event is not None:
            for name in ("get_platform_name", "get_group_id", "is_private_chat"):
                try:
                    self.snapshot[name] = getattr(event, name)()
                except Exception:
                    self.snapshot[name] = UNKNOWN

    def tick(self):
        self.steps += 1
        if self.steps > MAX_STEPS:
            self.incomplete = True
            return False
        return True

    def access_expr(self, node):
        if node not in self._access_cache:
            self._access_cache[node] = _access_expr(node)
        return self._access_cache[node]

    def merge_states(self, states):
        if len(states) <= 1:
            return states
        unique = {}
        for candidate in states:
            unique.setdefault(_state_key(candidate), candidate)
        if len(unique) > MAX_BRANCHES:
            self.incomplete = True
        return list(unique.values())[:MAX_BRANCHES]

    def literal(self, value):
        if isinstance(value, RuntimeValue):
            item = value.value
            if item is None or isinstance(item, (str, int, float, bool)):
                return item
            if type(item) in (dict, list, tuple, set, frozenset):
                if len(item) <= 512:
                    return item
            return value
        return value

    def truth(self, value):
        if isinstance(value, Access):
            return self.truth(value.value)
        if value is UNKNOWN or isinstance(
            value, (Text, Parts, Length, RuntimeValue, InstanceValue, MatchValue)
        ):
            return None
        if value is DENIED:
            return False
        if type(value) in (str, int, float, bool, tuple, list, set, frozenset, dict):
            return bool(value)
        if value is None:
            return False
        return None

    def bind(self, state, text, word):
        if not isinstance(word, str) or not word.strip() or len(word) > 160:
            return None
        words = word.strip().split()
        if tuple(words) in state.excluded.get(text.index, ()):
            return None
        if text.token and len(words) != 1:
            return None
        result = state.copy()
        for offset, part in enumerate(words):
            position = text.index + offset
            if part in result.excluded.get(position, ()):
                return None
            existing = result.route.get(position)
            if existing is not None and existing != part:
                return None
            result.route[position] = part
        result.matched = True
        result.delegated = False
        return result

    def route_name(self, state):
        if not state.route or 0 not in state.route:
            return "", True
        words = []
        for index in range(12):
            if index not in state.route:
                break
            words.append(state.route[index])
        raw = " ".join(words)
        return raw.removeprefix("/"), raw.startswith("/")

    def reject(self, state, permission=False):
        name, _ = self.route_name(state)
        if permission:
            if name:
                self.restricted.add(name)
            else:
                self.permission_rejected = True
        elif state.context_gate and not name:
            self.context_rejected = True

    def emit(self, state, function, value=UNKNOWN):
        if state.denied or state.permission_unknown or value is DENIED:
            self.reject(state, permission=True)
            return
        if state.delegated:
            return
        name, use_prefix = self.route_name(state)
        if not name or not state.matched or isinstance(value, (Text, Parts, Length)):
            return
        if len(self.entries) >= MAX_ENTRIES:
            self.incomplete = True
            return
        description = state.description or _summary(function.description)
        if not description:
            leaf = name.split()[-1]
            description = (
                "查看插件帮助。"
                if leaf.casefold() in {"help", "帮助", "菜单"}
                else f"{getattr(self.plugin, 'display_name', None) or self.plugin.name}的“{leaf}”功能。"
            )
        key = (name, use_prefix)
        entry = PublicEntry(
            name,
            description,
            state.usage,
            conditional=state.conditional,
            use_prefix=use_prefix,
        )
        previous = self.entries.get(key)
        if previous is None or (previous.conditional and not entry.conditional):
            self.entries[key] = entry

    def eval(self, node, state, function, depth):
        if node is None or not self.tick():
            return UNKNOWN
        value = self._eval(node, state, function, depth)
        if isinstance(value, Text) and value.skip:
            root = state.route.get(value.index)
            if root and value.skip >= len(root):
                return Text(value.index + 1, slash_stripped=value.slash_stripped)
        return value

    def _eval(self, node, state, function, depth):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in state.env:
                return state.env[node.id]
            value = self.index.resolve_name(function, node.id)
            if isinstance(value, ast.AST):
                if value is node or isinstance(value, ast.Name) and value.id == node.id:
                    return UNKNOWN
                return self.eval(value, state, function, depth)
            return self.literal(value) if value is not None else UNKNOWN
        if isinstance(node, ast.Attribute):
            owner = self.eval(node.value, state, function, depth)
            if isinstance(owner, EventValue):
                if node.attr in ("message_str", "message_text"):
                    return Text()
                return Operation(owner, node.attr)
            if isinstance(owner, (Text, Parts, MatchValue, RegexValue, str, dict)):
                return Operation(owner, node.attr)
            value = self.index.attribute(owner, node.attr)
            if value is not None:
                return self.literal(value)
            if node.attr == "config" and owner == self.index.self_value:
                return self.literal(
                    self.index.attribute(RuntimeValue(self.plugin), "config")
                )
            return Operation(owner, node.attr)
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            values = [self.eval(item, state, function, depth) for item in node.elts]
            if len(values) > 512:
                return UNKNOWN
            return tuple(values)
        if isinstance(node, ast.Dict):
            result = {}
            for key, value in zip(node.keys, node.values):
                key = self.eval(key, state, function, depth)
                if not isinstance(key, (str, int)):
                    return UNKNOWN
                result[key] = self.eval(value, state, function, depth)
            return result
        if isinstance(node, ast.Subscript):
            owner = self.literal(self.eval(node.value, state, function, depth))
            if isinstance(node.slice, ast.Slice):
                low = (
                    self.eval(node.slice.lower, state, function, depth)
                    if node.slice.lower
                    else 0
                )
                high = (
                    self.eval(node.slice.upper, state, function, depth)
                    if node.slice.upper
                    else None
                )
                if isinstance(owner, Text) and isinstance(low, int) and high is None:
                    return replace(owner, skip=owner.skip + low)
                if type(owner) in (tuple, list, str) and isinstance(low, int):
                    try:
                        return owner[low:high]
                    except (TypeError, IndexError):
                        return UNKNOWN
                return UNKNOWN
            key = self.eval(node.slice, state, function, depth)
            if isinstance(owner, Parts) and isinstance(key, int) and 0 <= key <= 24:
                return Text(owner.index + key, token=not (owner.tail and key == 1))
            if type(owner) in (dict, tuple, list, str) and isinstance(key, (str, int)):
                try:
                    return owner[key]
                except (KeyError, IndexError, TypeError):
                    return UNKNOWN
            if type(owner) is dict and isinstance(key, Text):
                known = state.route.get(key.index)
                if known in owner:
                    return owner[known]
            return UNKNOWN
        if isinstance(node, ast.Await):
            return self.eval(node.value, state, function, depth)
        if isinstance(node, (ast.Yield, ast.YieldFrom)):
            return self.eval(node.value, state, function, depth)
        if isinstance(node, ast.IfExp):
            yes, no = self.branches(node.test, state, function, depth)
            values = [self.eval(node.body, s, function, depth) for s in yes]
            values.extend(self.eval(node.orelse, s, function, depth) for s in no)
            texts = [item for item in values if isinstance(item, Text)]
            rooted = [item for item in texts if item.roots]
            return (
                rooted[0]
                if rooted
                else (texts[0] if texts else (values[0] if values else UNKNOWN))
            )
        if isinstance(node, ast.BoolOp):
            values = [self.eval(item, state, function, depth) for item in node.values]
            if any(isinstance(item, Access) for item in values):
                truths = [self.truth(item) for item in values]
                if isinstance(node.op, ast.Or) and any(t is True for t in truths):
                    return Access(True)
                if isinstance(node.op, ast.And) and any(t is False for t in truths):
                    return Access(False)
                if all(t is not None for t in truths):
                    return Access(
                        all(truths) if isinstance(node.op, ast.And) else any(truths)
                    )
                return Access()
            for item in values:
                if isinstance(item, Text):
                    return item
            if isinstance(node.op, ast.Or):
                return next(
                    (item for item in values if self.truth(item) is True), UNKNOWN
                )
            return values[-1] if all(self.truth(v) is True for v in values) else UNKNOWN
        if isinstance(node, ast.BinOp):
            left = self.eval(node.left, state, function, depth)
            right = self.eval(node.right, state, function, depth)
            if isinstance(node.op, ast.Add):
                if isinstance(left, str) and isinstance(right, str):
                    return (left + right)[:4096]
                if isinstance(left, Text):
                    return left
                if isinstance(right, Text):
                    if left and left.strip().startswith(("/", "!")):
                        return Text(max(0, right.index - 1), roots=(left.strip(),))
                    return right
            return UNKNOWN
        if isinstance(node, ast.UnaryOp):
            value = self.eval(node.operand, state, function, depth)
            truth = self.truth(value)
            if isinstance(node.op, ast.Not):
                result = not truth if truth is not None else UNKNOWN
                return Access(result) if isinstance(value, Access) else result
            if isinstance(node.op, ast.USub) and type(value) in (int, float):
                return -value
            return UNKNOWN
        if isinstance(node, ast.Compare):
            return self.compare_value(node, state, function, depth)
        if isinstance(node, ast.Call):
            return self.call(node, state, function, depth)
        return UNKNOWN

    def compare_value(self, node, state, function, depth):
        if len(node.ops) != 1:
            return UNKNOWN
        left = self.literal(self.eval(node.left, state, function, depth))
        right = self.literal(self.eval(node.comparators[0], state, function, depth))
        op = node.ops[0]
        access = self.access_expr(node)
        if isinstance(left, Text):
            known = state.route.get(left.index)
            if known is not None and left.token:
                left = known
        if isinstance(right, Text):
            known = state.route.get(right.index)
            if known is not None and right.token:
                right = known
        if isinstance(op, (ast.In, ast.NotIn)) and access:
            return Access(isinstance(op, ast.NotIn))
        if any(isinstance(item, Access) for item in (left, right)):
            left = left.value if isinstance(left, Access) else left
            right = right.value if isinstance(right, Access) else right

        def safe(value):
            return value is None or type(value) in (
                str,
                int,
                float,
                bool,
                tuple,
                list,
                dict,
                set,
                frozenset,
            )

        if not safe(left) or not safe(right):
            return Access() if access else UNKNOWN
        try:
            if isinstance(op, (ast.Eq, ast.Is)):
                result = left == right
            elif isinstance(op, (ast.NotEq, ast.IsNot)):
                result = left != right
            elif isinstance(op, ast.In):
                result = left in right
            elif isinstance(op, ast.NotIn):
                result = left not in right
            elif isinstance(op, ast.Gt):
                result = left > right
            elif isinstance(op, ast.GtE):
                result = left >= right
            elif isinstance(op, ast.Lt):
                result = left < right
            elif isinstance(op, ast.LtE):
                result = left <= right
            else:
                return UNKNOWN
            return Access(result) if access else result
        except (TypeError, ValueError):
            return Access() if access else UNKNOWN

    def call(self, node, state, function, depth):
        if isinstance(node.func, ast.Name) and node.func.id in (
            "len",
            "bool",
            "str",
            "dict",
            "list",
            "tuple",
        ):
            argument = (
                self.eval(node.args[0], state, function, depth)
                if node.args
                else UNKNOWN
            )
            if node.func.id == "len":
                return (
                    Length(argument)
                    if isinstance(argument, (Text, Parts))
                    else (
                        len(argument)
                        if type(argument) in (dict, list, tuple, str, set)
                        else UNKNOWN
                    )
                )
            if node.func.id == "bool":
                truth = self.truth(argument)
                value = truth if truth is not None else UNKNOWN
                return Access(value) if isinstance(argument, Access) else value
            if node.func.id in ("dict", "list", "tuple"):
                return argument
            return argument if isinstance(argument, Text) else UNKNOWN
        target = self.eval(node.func, state, function, depth)
        args = [self.eval(arg, state, function, depth) for arg in node.args]
        keywords = {
            kw.arg: self.eval(kw.value, state, function, depth)
            for kw in node.keywords
            if kw.arg
        }
        if isinstance(target, ClassValue):
            return self.index.construct(target)
        if isinstance(target, FunctionValue):
            if any(
                _restricted_decorator(decorator)
                for decorator in target.node.decorator_list
            ):
                self.reject(state, permission=True)
                return DENIED
            routed = any(isinstance(item, (Text, Parts, EventValue)) for item in args)
            if not routed:
                if _access_name(target.node.name):
                    return Access()
                return UNKNOWN
            child = state.copy()
            child.delegated = False
            child.env = {}
            parameters = [*target.node.args.posonlyargs, *target.node.args.args]
            if (
                target.owner is not None
                and parameters
                and parameters[0].arg in ("self", "cls")
            ):
                child.env[parameters.pop(0).arg] = target.owner
            for parameter, value in zip(parameters, args):
                child.env[parameter.arg] = value
            child.env.update(keywords)
            defaults = [None] * (
                len(parameters) - len(target.node.args.defaults)
            ) + list(target.node.args.defaults)
            for parameter, default in zip(parameters, defaults):
                if parameter.arg not in child.env:
                    child.env[parameter.arg] = (
                        self.eval(default, child, target, depth) if default else UNKNOWN
                    )
                if _access_name(parameter.arg):
                    value = child.env[parameter.arg]
                    child.env[parameter.arg] = (
                        value
                        if isinstance(value, Access)
                        else Access(False if isinstance(value, bool) else UNKNOWN)
                    )
            if target.description and any(isinstance(item, Text) for item in args):
                child.description = _summary(target.description)
            before_entries = len(self.entries)
            before_restricted = len(self.restricted)
            values = self.run(target, child, depth + 1)
            if (
                len(self.entries) != before_entries
                or len(self.restricted) != before_restricted
            ):
                state.delegated = True
            if _access_name(target.node.name):
                truths = [self.truth(item) for item in values]
                value = (
                    truths[0]
                    if truths and all(item == truths[0] for item in truths)
                    else UNKNOWN
                )
                return Access(value if value is not None else UNKNOWN)
            if values:
                texts = [item for item in values if isinstance(item, Text)]
                rooted = [item for item in texts if item.roots]
                return rooted[0] if rooted else (texts[0] if texts else values[0])
            return DENIED if self._restricted_path(state) else UNKNOWN
        if not isinstance(target, Operation):
            return UNKNOWN
        owner, name = target.owner, target.name
        if isinstance(owner, EventValue):
            if name in ("get_message_str",):
                return Text()
            if name in ("is_admin", "is_group_admin", "is_superuser"):
                return Access(False)
            if name in self.snapshot:
                self.index.context_reads[name] = self.snapshot[name]
                return self.snapshot[name]
            if name == "send" and args:
                self.emit(state, function)
            if name == "is_private_chat" and self.event is None:
                return UNKNOWN
            return UNKNOWN
        if isinstance(owner, Text):
            if name in ("strip", "lstrip", "rstrip", "lower", "casefold"):
                root = state.route.get(owner.index, "")
                if owner.skip and root and owner.skip >= len(root):
                    value = Text(owner.index + 1, slash_stripped=owner.slash_stripped)
                else:
                    value = owner
                if name == "lstrip" and args and args[0] == "/":
                    value = replace(value, slash_stripped=True)
                return value
            if name == "split":
                separator = args[0] if args else None
                limit = args[1] if len(args) > 1 else keywords.get("maxsplit")
                if separator in (None, " "):
                    return Parts(owner.index, tail=limit == 1)
            if name == "partition" and args == [" "]:
                return (Text(owner.index, token=True), " ", Text(owner.index + 1))
            if name in ("startswith", "endswith"):
                known = state.route.get(owner.index)
                if known and args and isinstance(args[0], str):
                    return (
                        known.startswith(args[0])
                        if name == "startswith"
                        else known.endswith(args[0])
                    )
                return UNKNOWN
        if isinstance(owner, str):
            if name in ("strip", "lower", "casefold"):
                return getattr(str, name)(owner)
            if name == "split" and (not args or args[0] in (None, " ")):
                return tuple(owner.split())
            if (
                name == "join"
                and args
                and type(args[0]) in (tuple, list)
                and all(isinstance(item, str) for item in args[0])
            ):
                return owner.join(args[0])[:4096]
            if (
                name in ("startswith", "endswith")
                and args
                and type(args[0]) in (str, tuple)
            ):
                return getattr(str, name)(owner, args[0])
        if isinstance(owner, dict) and name == "get" and args:
            key = args[0]
            if isinstance(key, Text):
                key = state.route.get(key.index, UNKNOWN)
            if type(key) in (str, int):
                return owner.get(key, args[1] if len(args) > 1 else UNKNOWN)
        if isinstance(owner, MatchValue) and name == "group" and args:
            if args[0] in (0, 1):
                return Text(owner.start, token=True)
            if args[0] == 2 and owner.has_args:
                return Text(owner.start + 1)
        if isinstance(owner, RegexValue) and name in ("match", "fullmatch", "search"):
            roots, has_args = _simple_regex(owner.pattern)
            if roots and args and isinstance(args[0], Text):
                return MatchValue(args[0].index, roots, has_args)
        if isinstance(node.func, ast.Attribute):
            module = node.func.value
            module_name = module.id if isinstance(module, ast.Name) else ""
            if module_name in ("re", "regex"):
                if name == "compile" and args and isinstance(args[0], str):
                    return RegexValue(args[0])
                if (
                    name in ("match", "fullmatch", "search")
                    and len(args) >= 2
                    and isinstance(args[0], str)
                    and isinstance(args[1], Text)
                ):
                    roots, has_args = _simple_regex(args[0])
                    if roots:
                        return MatchValue(args[1].index, roots, has_args)
            if (
                module_name == "shlex"
                and name in ("split", "shlex")
                and args
                and isinstance(args[0], Text)
            ):
                return Parts(args[0].index)
        if _access_name(name):
            return Access()
        return UNKNOWN

    def _restricted_path(self, state):
        name, _ = self.route_name(state)
        return bool(
            name
            and any(
                name == item or name.startswith(item + " ") for item in self.restricted
            )
        )

    def branches(self, node, state, function, depth):
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            yes, no = self.branches(node.operand, state, function, depth)
            return no, yes
        if isinstance(node, ast.BoolOp):
            pending, finished = [state.copy()], []
            for part in node.values:
                next_pending = []
                for item in pending[:MAX_BRANCHES]:
                    yes, no = self.branches(part, item, function, depth)
                    if isinstance(node.op, ast.And):
                        next_pending.extend(yes)
                        finished.extend(no)
                    else:
                        finished.extend(yes)
                        next_pending.extend(no)
                pending = self.merge_states(next_pending)
            return (
                (pending, self.merge_states(finished))
                if isinstance(node.op, ast.And)
                else (self.merge_states(finished), pending)
            )
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            left = self.eval(node.left, state, function, depth)
            right = self.eval(node.comparators[0], state, function, depth)
            op = node.ops[0]
            if (
                not isinstance(left, Text)
                and isinstance(right, Text)
                and isinstance(op, (ast.Eq, ast.NotEq))
            ):
                left, right = right, left
            if isinstance(left, Text) and isinstance(
                op, (ast.Eq, ast.NotEq, ast.In, ast.NotIn)
            ):
                words = _strings(right)
                if isinstance(op, (ast.Eq, ast.NotEq)) and not isinstance(right, str):
                    words = ()
                if words:
                    yes = [
                        item
                        for word in words
                        if (item := self.bind(state, left, word)) is not None
                    ]
                    no = state.copy()
                    no.excluded.setdefault(left.index, set()).update(
                        (
                            tuple(word.strip().split())
                            if not left.token
                            else word.strip()
                        )
                        for word in words
                        if word.strip()
                    )
                    known = state.route.get(left.index)
                    negatives = [] if known in no.excluded[left.index] else [no]
                    if isinstance(op, (ast.NotEq, ast.NotIn)):
                        return negatives, yes
                    return yes, negatives
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "startswith"
        ):
            owner = self.eval(node.func.value, state, function, depth)
            values = (
                self.eval(node.args[0], state, function, depth)
                if node.args
                else UNKNOWN
            )
            if isinstance(owner, Text):
                words = _strings(values)
                words = tuple(
                    word.rstrip()
                    for word in words
                    if word and (word.startswith(("/", "!")) or word.endswith(" "))
                )
                if words:
                    yes = [
                        item
                        for word in words
                        if (item := self.bind(state, owner, word)) is not None
                    ]
                    no = state.copy()
                    no.excluded.setdefault(owner.index, set()).update(words)
                    return yes, [no]
        value = self.eval(node, state, function, depth)
        if isinstance(value, MatchValue):
            yes = [
                item
                for word in value.roots
                if (item := self.bind(state, Text(value.start), word)) is not None
            ]
            for item in yes:
                if value.has_args:
                    item.usage = "[参数]"
            return yes, [state.copy()]
        truth = self.truth(value)
        if truth is not None:
            result = state.copy()
            if self._context_expr(node):
                result.context_gate = True
            return ([result], []) if truth else ([], [result])
        yes, no = state.copy(), state.copy()
        if isinstance(value, Access) or self.access_expr(node):
            yes.permission_unknown = no.permission_unknown = True
        else:
            yes.conditional = no.conditional = True
        if self._context_expr(node) and self.event is not None:
            yes.permission_unknown = no.permission_unknown = True
        return [yes], [no]

    def _context_expr(self, node):
        names = {
            item.attr if isinstance(item, ast.Attribute) else item.id
            for item in ast.walk(node)
            if isinstance(item, (ast.Name, ast.Attribute))
        }
        return bool(
            names
            & {
                "get_platform_name",
                "get_group_id",
                "is_private_chat",
                "allowed_groups",
                "allowed_users",
                "whitelist",
                "white_list",
                "platform",
            }
        )

    def assign(self, target, value, state):
        if isinstance(target, ast.Name):
            state.env[target.id] = value
        elif isinstance(target, (ast.Tuple, ast.List)):
            if isinstance(value, Parts):
                values = [
                    Text(value.index + i, token=not (value.tail and i == 1))
                    for i in range(len(target.elts))
                ]
            elif type(value) in (tuple, list):
                values = value
            else:
                values = [UNKNOWN] * len(target.elts)
            for item, part in zip(target.elts, values):
                self.assign(item, part, state)

    def terminal_denial(self, body):
        for item in body:
            if isinstance(item, (ast.Raise, ast.Return)):
                return True
        return False

    def run(self, function, state, depth=0):
        if depth > MAX_DEPTH or function.key in self.stack or not self.tick():
            self.incomplete = True
            return []
        self.stack.append(function.key)
        try:
            _, returns = self.block(function.node.body, [state], function, depth)
            return returns
        finally:
            self.stack.pop()

    def block(self, body, states, function, depth):
        returns = []
        states = self.merge_states(states)
        for statement in body:
            if not states or not self.tick():
                break
            next_states = []
            for state in states[:MAX_BRANCHES]:
                if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                    value = self.eval(statement.value, state, function, depth)
                    targets = (
                        statement.targets
                        if isinstance(statement, ast.Assign)
                        else [statement.target]
                    )
                    for target in targets:
                        self.assign(target, value, state)
                    next_states.append(state)
                elif isinstance(statement, ast.If):
                    yes, no = self.branches(statement.test, state, function, depth)
                    access = self.access_expr(statement.test)
                    if access and self.terminal_denial(statement.body):
                        for item in yes:
                            item.denied = True
                            self.reject(item, permission=True)
                    live, values = self.block(statement.body, yes, function, depth)
                    next_states.extend(live)
                    returns.extend(values)
                    live, values = self.block(statement.orelse, no, function, depth)
                    next_states.extend(live)
                    returns.extend(values)
                elif isinstance(statement, ast.Match):
                    subject = self.eval(statement.subject, state, function, depth)
                    if len(statement.cases) > MAX_BRANCHES:
                        self.incomplete = True
                    for case in statement.cases[:MAX_BRANCHES]:
                        patterns = (
                            case.pattern.patterns
                            if isinstance(case.pattern, ast.MatchOr)
                            else [case.pattern]
                        )
                        candidates = []
                        for pattern in patterns:
                            if isinstance(pattern, ast.MatchValue) and isinstance(
                                subject, Text
                            ):
                                value = self.eval(pattern.value, state, function, depth)
                                bound = self.bind(state, subject, value)
                                if bound:
                                    candidates.append(bound)
                            elif (
                                isinstance(pattern, ast.MatchAs)
                                and pattern.pattern is None
                            ):
                                candidates.append(state.copy())
                        if case.guard:
                            candidates = [
                                out
                                for candidate in candidates
                                for out in self.branches(
                                    case.guard, candidate, function, depth
                                )[0]
                            ]
                        live, values = self.block(
                            case.body, candidates, function, depth
                        )
                        next_states.extend(live)
                        returns.extend(values)
                elif isinstance(statement, ast.Return):
                    value = (
                        self.eval(statement.value, state, function, depth)
                        if statement.value is not None
                        else None
                    )
                    if value is not None and value is not DENIED and not state.denied:
                        self.emit(state, function, value)
                        returns.append(value)
                    else:
                        self.reject(
                            state,
                            permission=state.denied
                            or state.permission_unknown
                            or value is DENIED,
                        )
                elif isinstance(statement, ast.Raise):
                    self.reject(
                        state, permission=state.denied or state.permission_unknown
                    )
                elif isinstance(statement, ast.Expr):
                    value = self.eval(statement.value, state, function, depth)
                    if isinstance(statement.value, (ast.Yield, ast.YieldFrom)):
                        self.emit(state, function, value)
                    next_states.append(state)
                elif isinstance(statement, (ast.Try, ast.With, ast.AsyncWith)):
                    live, values = self.block(statement.body, [state], function, depth)
                    returns.extend(values)
                    if isinstance(statement, ast.Try):
                        live, values = self.block(
                            statement.orelse, live, function, depth
                        )
                        returns.extend(values)
                        live, values = self.block(
                            statement.finalbody, live, function, depth
                        )
                        returns.extend(values)
                    next_states.extend(live)
                elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    local = self.index.local_functions(function).get(statement.name)
                    if local:
                        state.env[statement.name] = local
                    next_states.append(state)
                elif isinstance(statement, (ast.For, ast.AsyncFor, ast.While)):
                    # Business loops are not command declarations; dynamic routers degrade.
                    state.conditional = True
                    next_states.append(state)
                elif isinstance(statement, (ast.Break, ast.Continue)):
                    continue
                else:
                    next_states.append(state)
            states = self.merge_states(next_states)
            expanded = []
            for state in states:
                roots = next(
                    (
                        value.roots
                        for value in state.env.values()
                        if isinstance(value, Text)
                        and value.roots
                        and value.index not in state.route
                    ),
                    (),
                )
                if roots:
                    for root in roots[:12]:
                        bound = self.bind(state, Text(), root)
                        if not bound:
                            continue
                        for key, value in bound.env.items():
                            if isinstance(value, Text) and value.roots:
                                bound.env[key] = replace(value, roots=())
                        expanded.append(bound)
                else:
                    expanded.append(state)
            # Unknown business conditions often converge to the same symbolic
            # environment. Analyze that state once instead of multiplying work.
            states = self.merge_states(expanded)
        return states, returns

    def scan(self):
        function = self.index.entry
        if function is None:
            return DiscoveryResult(incomplete=True)
        if any(
            _restricted_decorator(decorator)
            for decorator in function.node.decorator_list
        ):
            return DiscoveryResult(context_allowed=False)
        state = State()
        parameters = [*function.node.args.posonlyargs, *function.node.args.args]
        for parameter in parameters:
            if parameter.arg in ("self", "cls"):
                state.env[parameter.arg] = self.index.self_value
            else:
                state.env[parameter.arg] = EventValue()
                break
        registered_roots = command_roots(self.handler)
        starts = (
            [
                bound
                for name in registered_roots
                if (bound := self.bind(state, Text(), "/" + name))
            ]
            if registered_roots
            else [state]
        )
        if registered_roots:
            event_position = next(
                (
                    i
                    for i, parameter in enumerate(parameters)
                    if parameter.arg not in ("self", "cls")
                ),
                len(parameters),
            )
            for candidate in starts:
                offset = len(candidate.route)
                for index, parameter in enumerate(parameters[event_position + 1 :]):
                    candidate.env[parameter.arg] = Text(offset + index, token=True)
        for item in getattr(self.handler, "event_filters", ()):
            if not any(cls.__name__ == "RegexFilter" for cls in type(item).__mro__):
                continue
            pattern = getattr(item, "regex_str", None)
            if pattern is None:
                pattern = getattr(getattr(item, "regex", None), "pattern", None)
            if not isinstance(pattern, str):
                return DiscoveryResult(incomplete=True)
            roots, has_args = _simple_regex(pattern)
            if not roots:
                return DiscoveryResult(incomplete=True)
            starts = [
                bound
                for candidate in starts
                for root in roots
                if (bound := self.bind(candidate, Text(), root)) is not None
            ]
            if has_args:
                for candidate in starts:
                    candidate.usage = "[参数]"
        self.started = True
        for candidate in starts:
            self.run(function, candidate)
        filtered = []
        for entry in self.entries.values():
            if registered_roots and not any(
                entry.command == root or entry.command.startswith(root + " ")
                for root in registered_roots
            ):
                continue
            if any(
                entry.command == command or entry.command.startswith(command + " ")
                for command in self.restricted
            ):
                continue
            filtered.append(entry)
        allowed = bool(filtered) or not (
            self.context_rejected or self.permission_rejected
        )
        return DiscoveryResult(
            tuple(
                sorted(
                    filtered,
                    key=lambda item: (item.command.casefold(), not item.use_prefix),
                )
            ),
            tuple(sorted(self.restricted)),
            allowed,
            self.incomplete or bool(self.index.issues),
        )


def discover(plugin, handler, event=None):
    """Never import, execute, or ask a plugin to enumerate its private routes."""
    try:
        snapshot = {}
        if event is not None:
            for name in ("get_platform_name", "get_group_id", "is_private_chat"):
                try:
                    value = getattr(event, name)()
                    snapshot[name] = (
                        value if type(value) in (str, int, bool) else UNKNOWN
                    )
                except Exception:
                    snapshot[name] = UNKNOWN
        context = tuple(snapshot.items()) if event is not None else None

        def compute():
            scanner = Scanner(plugin, handler, event, snapshot=snapshot)
            return scanner.scan(), scanner.index

        return _RESULTS.evaluate(plugin, handler, context, compute)
    except (
        OSError,
        SyntaxError,
        ValueError,
        TypeError,
        RecursionError,
        AttributeError,
    ):
        return DiscoveryResult(incomplete=True)


def cached_discovery(plugin, handler):
    """Dropdown refreshes only consume existing results; they never analyze source."""
    return _RESULTS.peek(plugin, handler) or DiscoveryResult()


def invalidate_discovery(plugin=None):
    _RESULTS.invalidate(plugin)


def discovery_revision():
    return _RESULTS.content_revision

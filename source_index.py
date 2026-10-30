"""Bounded, non-executing references to a loaded listener's local Python source.

Only source beneath the listener's plugin root is considered.  Values describe
references, not evaluated plugin objects; in particular, constructing an
InstanceValue never calls the corresponding Python constructor.
"""

from __future__ import annotations

import ast
import functools
import stat
import sys
import tokenize
import types
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock

MAX_FILES = 64
MAX_FILE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 6 * 1024 * 1024
_MAX_CACHE_ENTRIES = 32
_MAX_DEPTH = 64
_MISSING = object()
_PRIMITIVES = (str, bytes, int, float, complex, bool, type(None))
# Use the built-in type descriptors, not a plugin metaclass's getters.
_TYPE_DICT = type.__dict__["__dict__"]
_TYPE_MRO = type.__dict__["__mro__"]


@dataclass(frozen=True)
class ModuleValue:
    path: Path
    # A virtual prefix supports ``import data.plugins.demo.commands`` without
    # opening the ancestor packages outside the permitted source root.
    prefix: tuple[str, ...] = field(default=(), repr=False)


@dataclass(frozen=True)
class ClassValue:
    module: ModuleValue
    name: str


@dataclass(frozen=True)
class InstanceValue:
    cls: ClassValue


@dataclass(frozen=True, eq=False)
class RuntimeValue:
    value: object


@dataclass(frozen=True)
class FunctionValue:
    module: ModuleValue
    node: ast.FunctionDef | ast.AsyncFunctionDef
    owner: InstanceValue | RuntimeValue | None = None
    parent: FunctionValue | None = field(default=None, repr=False, compare=False)
    class_name: str | None = field(default=None, repr=False)
    bound_name: str | None = field(default=None, repr=False)

    @property
    def key(self) -> tuple[str, int]:
        return str(self.module.path), self.node.lineno

    @property
    def description(self) -> str:
        text = ast.get_docstring(self.node, clean=True) or ""
        return text.splitlines()[0].strip()[:160] if text else ""


ResolvedValue = (
    FunctionValue
    | ClassValue
    | ModuleValue
    | InstanceValue
    | RuntimeValue
    | ast.AST
    | str
    | bytes
    | int
    | float
    | complex
    | bool
    | None
)


@dataclass(frozen=True)
class _Import:
    module: str
    member: str | None = None
    level: int = 0
    bind_top: bool = False


@dataclass(frozen=True)
class _Definition:
    node: ast.FunctionDef | ast.AsyncFunctionDef
    qualname: str
    class_name: str | None


@dataclass
class _Parsed:
    tree: ast.Module
    symbols: dict[str, ast.AST | _Import]
    functions: list[_Definition]
    classes: dict[str, ast.ClassDef]


_CACHE: OrderedDict[tuple[str, int, int, int], _Parsed] = OrderedDict()
_CACHE_LOCK = RLock()


def _class_dict(cls: type) -> types.MappingProxyType:
    return _TYPE_DICT.__get__(cls, type(cls))


def _mro(cls: type) -> tuple[type, ...]:
    return _TYPE_MRO.__get__(cls, type(cls))


def _object_dict(value: object) -> dict:
    """Read the real instance dictionary without calling user descriptors."""
    for cls in _mro(type(value)):
        descriptor = _class_dict(cls).get("__dict__", _MISSING)
        if descriptor is not _MISSING:
            if not isinstance(
                descriptor, (types.GetSetDescriptorType, types.MemberDescriptorType)
            ):
                return {}
            result = object.__getattribute__(value, "__dict__")
            return result if type(result) is dict else {}
    return {}


def _stored(value: object, name: str, default: object = None) -> object:
    try:
        return _object_dict(value).get(name, default)
    except Exception:
        return default


def file_stamp(path):
    """A cheap dependency check; never open source just to validate a result."""
    try:
        info = Path(path).stat()
        return info.st_mtime_ns, info.st_size, info.st_ino, info.st_mode
    except (OSError, ValueError):
        return None


def freeze_value(value, budget=None, depth=0):
    """Snapshot stored builtin values without invoking plugin methods."""
    budget = [4096] if budget is None else budget
    budget[0] -= 1
    if budget[0] < 0 or depth > 12:
        raise ValueError("runtime dependency is too large to cache safely")
    if type(value) in _PRIMITIVES:
        return type(value), value
    if isinstance(value, dict):
        return (
            "dict",
            tuple(
                (freeze_value(k, budget, depth + 1), freeze_value(v, budget, depth + 1))
                for k, v in dict.items(value)
            ),
        )
    if type(value) in (list, tuple, set, frozenset):
        return type(value), tuple(freeze_value(v, budget, depth + 1) for v in value)
    # Opaque services are followed through separately recorded attribute reads.
    return type(value), id(value)


def runtime_stamp(value, name):
    stored = _stored(value, name, _MISSING)
    if stored is _MISSING:
        for cls in _mro(type(value)):
            namespace = _class_dict(cls)
            if name in namespace:
                stored = namespace[name]
                if isinstance(stored, types.MemberDescriptorType):
                    try:
                        stored = stored.__get__(value, type(value))
                    except AttributeError:
                        stored = _MISSING
                break
    return freeze_value(stored)


def _is_owner(value: object) -> bool:
    return (
        value is not None
        and type(value) not in _PRIMITIVES
        and not isinstance(
            value,
            (
                type,
                types.ModuleType,
                types.FunctionType,
                types.MethodType,
                functools.partial,
            ),
        )
    )


def _unwrap(value: object) -> tuple[types.FunctionType | None, object | None]:
    owner = None
    seen: set[int] = set()
    for _ in range(_MAX_DEPTH):
        if id(value) in seen:
            return None, owner
        seen.add(id(value))
        if isinstance(value, functools.partial):
            args = functools.partial.args.__get__(value, type(value))
            if owner is None and args and _is_owner(args[0]):
                owner = args[0]
            value = functools.partial.func.__get__(value, type(value))
        elif isinstance(value, types.MethodType):
            bound = object.__getattribute__(value, "__self__")
            if _is_owner(bound):
                owner = bound
            value = object.__getattribute__(value, "__func__")
        else:
            return (value if isinstance(value, types.FunctionType) else None), owner
    return None, owner


def _first_arg(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    args = [*node.args.posonlyargs, *node.args.args]
    return args[0].arg if args else None


def _walk_scope(body: list[ast.stmt]):
    """Walk one lexical scope, excluding nested functions and classes."""
    stack: list[ast.AST] = list(reversed(body))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
        ):
            continue
        stack.extend(reversed(list(ast.iter_child_nodes(node))))


def _imports(node: ast.Import | ast.ImportFrom) -> dict[str, _Import]:
    result = {}
    for item in node.names:
        if item.name == "*":
            continue
        if isinstance(node, ast.Import):
            result[item.asname or item.name.split(".")[0]] = _Import(
                item.name, bind_top=item.asname is None
            )
        else:
            result[item.asname or item.name] = _Import(
                node.module or "", item.name, node.level
            )
    return result


def _parse(source: str, path: Path) -> _Parsed:
    tree = ast.parse(source, filename=str(path))
    symbols: dict[str, ast.AST | _Import] = {}
    # Deliberately do not assume which top-level conditional import branch ran.
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            symbols[node.name] = node
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            symbols.update(_imports(node))
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    symbols[target.id] = node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value
        ):
            symbols[node.target.id] = node.value
        elif isinstance(node, ast.Delete):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    symbols.pop(target.id, None)

    functions = []
    classes = {}
    stack = [(node, (), None) for node in reversed(tree.body)]
    while stack:
        node, scope, direct_class = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qualname = ".".join((*scope, node.name))
            functions.append(_Definition(node, qualname, direct_class))
            stack.extend(
                (child, (*scope, node.name), None) for child in reversed(node.body)
            )
        elif isinstance(node, ast.ClassDef):
            qualname = ".".join((*scope, node.name))
            classes[qualname] = node
            stack.extend(
                (child, (*scope, node.name), qualname) for child in reversed(node.body)
            )
        else:
            stack.extend(
                (child, scope, direct_class)
                for child in reversed(list(ast.iter_child_nodes(node)))
            )
    return _Parsed(tree, symbols, functions, classes)


class SourceIndex:
    """Resolve only reachable local imports, functions and explicit constructors.

    Public lookup methods fail closed, recording short strings in ``issues``.
    ``file_count`` counts distinct source files admitted to the read budget,
    including unreadable/invalid source and cache hits.  No plugin import, call,
    constructor, property, or dynamic attribute hook is executed.
    """

    def __init__(self, plugin: object, handler: object):
        self.entry: FunctionValue | None = None
        self.self_value: InstanceValue | RuntimeValue | None = None
        self.issues: list[str] = []
        self.file_count = 0
        self.root: Path | None = None
        self._package_names: set[str] = set()
        self._modules: dict[str, tuple[tuple[str, int, int, int], _Parsed | None]] = {}
        self._files: set[str] = set()
        self._charged: set[tuple[str, int, int, int]] = set()
        self._bytes = 0
        self._active: set[tuple] = set()
        self._attribute_cache: dict[tuple, dict[str, ClassValue | None]] = {}
        self._name_cache = {}
        self._resolved_attributes = {}
        self._local_cache = {}
        self._scope_cache = {}
        self._bindings_cache = {}
        self._filename_cache = {}
        self.watched_files = {}
        self.runtime_dependencies = {}
        self.context_reads = {}
        self.cacheable = True
        try:
            callable_value = _stored(handler, "handler", _MISSING)
            if callable_value is _MISSING:
                callable_value = handler
            function, owner = _unwrap(callable_value)
            if owner is None:
                candidate = _stored(plugin, "star_cls")
                if _is_owner(candidate):
                    owner = candidate
            if owner is not None:
                self.self_value = RuntimeValue(owner)
            if function is None:
                self._issue("listener is not a Python function")
                return
            filename = object.__getattribute__(function, "__code__").co_filename
            path = self._filename(filename)
            if path is None:
                self._issue("listener source is unavailable")
                return
            self.root, module_name, module_file = self._source_root(plugin, path)
            if self.root is None or not self._inside(path):
                self._issue("listener source is outside the plugin root")
                return
            self._add_package_name(module_name, module_file)
            self._add_package_name(
                object.__getattribute__(function, "__module__"), path
            )
            if self.root.name.isidentifier():
                self._package_names.add(self.root.name)
            self.entry = self._python_function(function, self.self_value)
            if self.entry is None:
                return
            if self.self_value is None and self.entry.class_name:
                self.self_value = InstanceValue(
                    ClassValue(self.entry.module, self.entry.class_name)
                )
                self.entry = FunctionValue(
                    self.entry.module,
                    self.entry.node,
                    self.self_value,
                    class_name=self.entry.class_name,
                    bound_name=_first_arg(self.entry.node),
                )
        except Exception as exc:
            self._issue(f"listener source unavailable ({type(exc).__name__})")
            self.entry = None

    def _issue(self, message: str) -> None:
        if len(self.issues) < 128 and message not in self.issues:
            self.issues.append(message)

    def _filename(self, filename: object) -> Path | None:
        if (
            type(filename) is not str
            or not filename
            or filename.startswith("<")
            or "\0" in filename
        ):
            return None
        if filename in self._filename_cache:
            return self._filename_cache[filename]
        self.watched_files.setdefault(filename, file_stamp(filename))
        try:
            path = Path(filename).resolve(strict=True)
            result = path if path.is_file() else None
        except (OSError, ValueError, RuntimeError):
            result = None
        self._filename_cache[filename] = result
        return result

    def _inside(self, path: Path) -> bool:
        return self.root is not None and path.is_relative_to(self.root)

    def _source_root(
        self, plugin: object, entry: Path
    ) -> tuple[Path | None, str | None, Path | None]:
        module = _stored(plugin, "module")
        name = _stored(plugin, "module_path")
        if type(name) is not str:
            name = _stored(module, "__name__")
        if type(name) is not str:
            name = None
        if not isinstance(module, types.ModuleType) and name:
            module = sys.modules.get(name)
        module_file = self._filename(_stored(module, "__file__"))
        if module_file:
            return module_file.parent, name, module_file
        if name and ("/" in name or "\\" in name or name.endswith(".py")):
            try:
                candidate = Path(name).resolve(strict=True)
                if candidate.is_dir():
                    return candidate, None, None
                if candidate.is_file():
                    return candidate.parent, None, candidate
            except (OSError, ValueError, RuntimeError):
                pass
        if name and all(part.isidentifier() for part in name.split(".")):
            parts = name.split(".")
            # Metadata normally identifies <plugin-package>.main.  Anchor that
            # package to a real ancestor, even if the listener is in a submodule.
            for parent in entry.parents:
                if len(parts) > 1 and parent.name == parts[-2]:
                    local_file = self._filename(str(parent / (parts[-1] + ".py")))
                    if local_file:
                        return parent, name, local_file
                    local_file = self._filename(str(parent / parts[-1] / "__init__.py"))
                    if local_file:
                        return local_file.parent, name, local_file
                if parent.name == parts[-1]:
                    return parent, name, parent / "__init__.py"
        return entry.parent, None, None

    def _add_package_name(self, name: object, file: Path | None) -> None:
        if type(name) is not str or file is None or not self._inside(file):
            return
        parts = name.split(".")
        if not all(part.isidentifier() for part in parts):
            return
        relative = file.relative_to(self.root)
        suffix = list(relative.parts[:-1])
        if file.name != "__init__.py":
            suffix.append(file.stem)
        if suffix and parts[-len(suffix) :] == suffix:
            parts = parts[: -len(suffix)]
        elif suffix:
            return
        if parts:
            self._package_names.add(".".join(parts))

    def _load(self, module: ModuleValue) -> _Parsed | None:
        if module.prefix or self.root is None:
            return None
        # Each index owns one consistent source snapshot. Repeated lookups need
        # neither resolve() nor stat(); subsequent scans validate dependencies.
        previous = self._modules.get(str(module.path))
        if previous is not None:
            return previous[1]
        try:
            path = module.path.resolve(strict=True)
            if not self._inside(path):
                self._issue(f"source outside plugin root: {path}")
                return None
            info = path.stat()
            self.watched_files.setdefault(str(path), file_stamp(path))
            if not stat.S_ISREG(info.st_mode):
                return None
            if info.st_size > MAX_FILE_BYTES:
                self._issue(f"source exceeds 1 MiB: {path}")
                return None
            key = (str(path), info.st_mtime_ns, info.st_size, info.st_ino)
            previous = self._modules.get(str(path))
            if previous and previous[0] == key:
                return previous[1]
            if key not in self._charged:
                if str(path) not in self._files and len(self._files) >= MAX_FILES:
                    self._issue("source file budget exceeded (64 files)")
                    return None
                if self._bytes + info.st_size > MAX_TOTAL_BYTES:
                    self._issue("source byte budget exceeded (6 MiB)")
                    return None
                self._files.add(str(path))
                self.file_count = len(self._files)
                self._charged.add(key)
                self._bytes += info.st_size
            with _CACHE_LOCK:
                parsed = _CACHE.get(key)
                if parsed is not None:
                    _CACHE.move_to_end(key)
            if parsed is None:
                with tokenize.open(str(path)) as source_file:
                    source = source_file.read(MAX_FILE_BYTES + 1)
                current = path.stat()
                if (
                    len(source) > MAX_FILE_BYTES
                    or current.st_size > MAX_FILE_BYTES
                    or (current.st_mtime_ns, current.st_size)
                    != (info.st_mtime_ns, info.st_size)
                    or not self._inside(path.resolve(strict=True))
                ):
                    self._issue(f"source changed while reading: {path}")
                    return None
                parsed = _parse(source, path)
                with _CACHE_LOCK:
                    _CACHE[key] = parsed
                    _CACHE.move_to_end(key)
                    while len(_CACHE) > _MAX_CACHE_ENTRIES:
                        _CACHE.popitem(last=False)
            self._modules[str(path)] = (key, parsed)
            return parsed
        except Exception as exc:
            self._issue(f"cannot read source: {module.path} ({type(exc).__name__})")
            # Cache failed reads only for this index and this exact file version.
            if "key" in locals():
                self._modules[str(path)] = (key, None)
            return None

    def _module_at(self, parts: list[str]) -> ModuleValue | None:
        if self.root is None or any(not part.isidentifier() for part in parts):
            return None
        base = self.root.joinpath(*parts)
        candidates = (
            [base / "__init__.py"]
            if not parts
            else [base.with_suffix(".py"), base / "__init__.py"]
        )
        for candidate in candidates:
            path = self._filename(str(candidate))
            if path is not None and self._inside(path):
                return ModuleValue(path)
        try:
            self.watched_files.setdefault(str(base), file_stamp(base))
            path = base.resolve(strict=True)
            if self._inside(path) and path.is_dir():
                return ModuleValue(path)
        except (OSError, ValueError, RuntimeError):
            pass
        return None

    def _absolute_module(self, name: str) -> ModuleValue | None:
        parts = name.split(".") if name else []
        if any(not part.isidentifier() for part in parts):
            return None
        for package in sorted(self._package_names, key=len, reverse=True):
            prefix = package.split(".")
            if parts == prefix[: len(parts)] and len(parts) < len(prefix):
                return ModuleValue(self.root, tuple(prefix[len(parts) :]))
            if parts[: len(prefix)] == prefix:
                return self._module_at(parts[len(prefix) :])
        return self._module_at(parts)

    def _import_value(self, source: ModuleValue, ref: _Import) -> ResolvedValue:
        if ref.level:
            parent = source.path if source.path.is_dir() else source.path.parent
            parts = list(parent.relative_to(self.root).parts)
            if ref.level - 1 > len(parts):
                self._issue("relative import leaves plugin root")
                return None
            if ref.level > 1:
                parts = parts[: -(ref.level - 1)]
            module = self._module_at(
                parts + (ref.module.split(".") if ref.module else [])
            )
        else:
            module = self._absolute_module(ref.module)
        if module is None:
            return None
        if ref.member is not None:
            return self._module_attribute(module, ref.member)
        if ref.bind_top:
            return self._absolute_module(ref.module.split(".")[0])
        return module

    def _python_function(
        self,
        function: types.FunctionType,
        owner: InstanceValue | RuntimeValue | None = None,
    ) -> FunctionValue | None:
        code = object.__getattribute__(function, "__code__")
        path = self._filename(code.co_filename)
        if path is None or not self._inside(path):
            self._issue("Python function source unavailable or outside plugin root")
            return None
        module = ModuleValue(path)
        parsed = self._load(module)
        if parsed is None:
            return None
        name = object.__getattribute__(function, "__name__")
        qualname = object.__getattribute__(function, "__qualname__").replace(
            ".<locals>.", "."
        )
        candidates = []
        for definition in parsed.functions:
            node = definition.node
            start = min([node.lineno, *(item.lineno for item in node.decorator_list)])
            if node.name == name and start <= code.co_firstlineno <= node.lineno:
                candidates.append(definition)
        exact = [item for item in candidates if item.qualname == qualname]
        if len(exact or candidates) != 1:
            self._issue(
                f"cannot locate listener/function AST: {path}:{code.co_firstlineno}"
            )
            return None
        definition = (exact or candidates)[0]
        return FunctionValue(
            module,
            definition.node,
            owner,
            class_name=definition.class_name,
            bound_name=_first_arg(definition.node) if owner is not None else None,
        )

    def _guard(self, key: tuple) -> bool:
        if len(self._active) >= _MAX_DEPTH or key in self._active:
            self._issue("source reference cycle/depth limit")
            return False
        self._active.add(key)
        return True

    def _global(self, module: ModuleValue, name: str) -> ResolvedValue:
        key = ("global", str(module.path), name)
        if not self._guard(key):
            return None
        try:
            parsed = self._load(module)
            if parsed is None:
                return None
            value = parsed.symbols.get(name)
            if isinstance(value, _Import):
                return self._import_value(module, value)
            if isinstance(value, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return FunctionValue(module, value)
            if isinstance(value, ast.ClassDef):
                return ClassValue(module, value.name)
            if isinstance(value, ast.Constant):
                return value.value if type(value.value) in _PRIMITIVES else None
            return value
        finally:
            self._active.discard(key)

    def local_functions(self, function: FunctionValue) -> dict[str, FunctionValue]:
        key = function.key, id(function.owner)
        if key in self._local_cache:
            return self._local_cache[key]
        result = self._local_functions(function)
        self._local_cache[key] = result
        return result

    def _local_functions(self, function: FunctionValue) -> dict[str, FunctionValue]:
        try:
            if self._load(function.module) is None:
                return {}
            return {
                node.name: FunctionValue(
                    function.module, node, function.owner, parent=function
                )
                for node in self._scope(function.node)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
        except Exception as exc:
            self._issue(f"local function lookup failed ({type(exc).__name__})")
            return {}

    def _scope(self, node):
        if node not in self._scope_cache:
            self._scope_cache[node] = tuple(_walk_scope(node.body))
        return self._scope_cache[node]

    def _bindings(self, node):
        if node in self._bindings_cache:
            return self._bindings_cache[node]
        global_names, nonlocal_names, local_names, local_imports = (
            set(),
            set(),
            set(),
            {},
        )
        for item in self._scope(node):
            if isinstance(item, ast.Global):
                global_names.update(item.names)
            elif isinstance(item, ast.Nonlocal):
                nonlocal_names.update(item.names)
            elif isinstance(item, (ast.Import, ast.ImportFrom)):
                bindings = _imports(item)
                local_names.update(bindings)
                if item in node.body:
                    local_imports.update(bindings)
            elif isinstance(item, ast.Name) and isinstance(
                item.ctx, (ast.Store, ast.Del)
            ):
                local_names.add(item.id)
            elif isinstance(
                item, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                local_names.add(item.name)
            elif isinstance(item, ast.ExceptHandler) and item.name:
                local_names.add(item.name)
        result = global_names, nonlocal_names, local_names, local_imports
        self._bindings_cache[node] = result
        return result

    def resolve_name(self, function: FunctionValue, name: str) -> ResolvedValue:
        key = function.key, id(function.owner), name
        if key in self._name_cache:
            return self._name_cache[key]
        result = self._resolve_name(function, name)
        self._name_cache[key] = result
        return result

    def _resolve_name(self, function: FunctionValue, name: str) -> ResolvedValue:
        try:
            if self._load(function.module) is None or type(name) is not str:
                return None
            key = ("name", *function.key, name)
            if not self._guard(key):
                return None
            try:
                args = function.node.args
                parameters = {
                    arg.arg for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]
                }
                parameters.update(arg.arg for arg in (args.vararg, args.kwarg) if arg)
                bound = function.bound_name
                if (
                    bound is None
                    and function.parent is None
                    and function.owner is not None
                ):
                    bound = _first_arg(function.node)
                if name == bound:
                    return function.owner
                if name in parameters:
                    return None
                nested = self.local_functions(function)
                if name in nested:
                    return nested[name]
                global_names, nonlocal_names, local_names, local_imports = (
                    self._bindings(function.node)
                )
                if name in global_names:
                    return self._global(function.module, name)
                if name in local_names and name not in nonlocal_names:
                    ref = local_imports.get(name)
                    return (
                        self._import_value(function.module, ref)
                        if ref is not None
                        else None
                    )
                if function.parent is not None:
                    return self.resolve_name(function.parent, name)
                return self._global(function.module, name)
            finally:
                self._active.discard(key)
        except Exception as exc:
            self._issue(f"name lookup failed ({type(exc).__name__})")
            return None

    def _module_attribute(self, module: ModuleValue, name: str) -> ResolvedValue:
        if module.prefix:
            return (
                ModuleValue(module.path, module.prefix[1:])
                if name == module.prefix[0]
                else None
            )
        result = self._global(module, name)
        if result is not None:
            return result
        parent = module.path if module.path.is_dir() else module.path.parent
        if module.path.is_file() and module.path.name != "__init__.py":
            return None
        return self._module_at([*parent.relative_to(self.root).parts, name])

    def _class_node(self, cls: ClassValue) -> ast.ClassDef | None:
        parsed = self._load(cls.module)
        return parsed.classes.get(cls.name) if parsed else None

    @staticmethod
    def _descriptor_method(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
        for decorator in node.decorator_list:
            item = decorator.func if isinstance(decorator, ast.Call) else decorator
            name = (
                item.id
                if isinstance(item, ast.Name)
                else item.attr
                if isinstance(item, ast.Attribute)
                else ""
            )
            if name in {"property", "cached_property", "setter", "deleter", "getter"}:
                return True
        return False

    def _class_attribute(
        self,
        cls: ClassValue,
        name: str,
        owner: InstanceValue | RuntimeValue | None = None,
    ) -> ResolvedValue:
        node = self._class_node(cls)
        if node is None:
            return None
        for item in reversed(node.body):
            if (
                isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                and item.name == name
            ):
                if self._descriptor_method(item):
                    return None
                static = any(
                    isinstance(d, ast.Name) and d.id in {"staticmethod", "classmethod"}
                    for d in item.decorator_list
                )
                return FunctionValue(
                    cls.module,
                    item,
                    None if static else owner,
                    class_name=cls.name,
                    bound_name=_first_arg(item)
                    if owner is not None and not static
                    else None,
                )
            if isinstance(item, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in item.targets
            ):
                return (
                    item.value.value
                    if isinstance(item.value, ast.Constant)
                    else item.value
                )
            if (
                isinstance(item, ast.AnnAssign)
                and isinstance(item.target, ast.Name)
                and item.target.id == name
                and item.value
            ):
                return (
                    item.value.value
                    if isinstance(item.value, ast.Constant)
                    else item.value
                )
        return None

    def _static_reference(
        self, function: FunctionValue, expression: ast.AST
    ) -> ResolvedValue:
        key = ("expression", *function.key, id(expression))
        if not self._guard(key):
            return None
        try:
            if isinstance(expression, ast.Name):
                value = self.resolve_name(function, expression.id)
                return (
                    self._static_reference(function, value)
                    if isinstance(value, ast.AST)
                    else value
                )
            if isinstance(expression, ast.Attribute):
                value = self._static_reference(function, expression.value)
                result = self.attribute(value, expression.attr)
                return (
                    self._static_reference(function, result)
                    if isinstance(result, ast.AST)
                    else result
                )
            if isinstance(expression, ast.Call):
                value = self._static_reference(function, expression.func)
                return InstanceValue(value) if isinstance(value, ClassValue) else None
            if (
                isinstance(expression, ast.Constant)
                and type(expression.value) in _PRIMITIVES
            ):
                return expression.value
            return None
        finally:
            self._active.discard(key)

    def _instance_types(self, cls: ClassValue) -> dict[str, ClassValue | None]:
        node = self._class_node(cls)
        if node is None:
            return {}
        version = self._modules[str(cls.module.path.resolve(strict=True))][0]
        key = (version, cls.name)
        if key in self._attribute_cache:
            return self._attribute_cache[key]
        attributes: dict[str, ClassValue | None] = {}
        for method in node.body:
            if not isinstance(
                method, (ast.FunctionDef, ast.AsyncFunctionDef)
            ) or self._descriptor_method(method):
                continue
            first = _first_arg(method)
            if first is None or first != "self":
                continue
            function = FunctionValue(
                cls.module,
                method,
                InstanceValue(cls),
                class_name=cls.name,
                bound_name=first,
            )
            for assignment in _walk_scope(method.body):
                targets = []
                value = None
                if isinstance(assignment, ast.Assign):
                    targets, value = assignment.targets, assignment.value
                elif isinstance(assignment, (ast.AnnAssign, ast.AugAssign)):
                    targets, value = [assignment.target], assignment.value
                elif isinstance(assignment, ast.Delete):
                    targets = assignment.targets
                if isinstance(value, ast.Constant) and value.value is None:
                    continue
                for target in targets:
                    if not (
                        isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == first
                    ):
                        continue
                    inferred = None
                    if isinstance(value, ast.Call) and not isinstance(
                        assignment, ast.AugAssign
                    ):
                        constructor = self._static_reference(function, value.func)
                        if isinstance(constructor, ClassValue):
                            inferred = constructor
                    if target.attr not in attributes:
                        attributes[target.attr] = inferred
                    elif attributes[target.attr] != inferred:
                        attributes[target.attr] = None
        # Once conflicting/unknown writes appear, a subsequent write cannot
        # restore certainty: the accumulator above leaves the result at None.
        self._attribute_cache[key] = attributes
        return attributes

    def _instance_attribute(self, instance: InstanceValue, name: str) -> ResolvedValue:
        key = ("instance", str(instance.cls.module.path), instance.cls.name, name)
        if not self._guard(key):
            return None
        try:
            result = self._class_attribute(instance.cls, name, instance)
            if result is not None:
                return result
            cls = self._instance_types(instance.cls).get(name)
            return InstanceValue(cls) if cls is not None else None
        finally:
            self._active.discard(key)

    def _runtime_class(self, value: object) -> ClassValue | None:
        cls = value if isinstance(value, type) else type(value)
        namespace = _class_dict(cls)
        name = namespace.get("__qualname__")
        if type(name) is not str:
            name = type.__getattribute__(cls, "__qualname__")
        if type(name) is not str:
            return None
        name = name.replace(".<locals>.", ".")
        paths = []
        module = sys.modules.get(namespace.get("__module__"))
        path = self._filename(_stored(module, "__file__"))
        if path is not None and self._inside(path):
            paths.append(path)
        for item in namespace.values():
            if isinstance(item, types.FunctionType):
                path = self._filename(
                    object.__getattribute__(item, "__code__").co_filename
                )
                if path is not None and self._inside(path) and path not in paths:
                    paths.append(path)
        for path in paths:
            module_value = ModuleValue(path)
            parsed = self._load(module_value)
            if parsed is not None and name in parsed.classes:
                return ClassValue(module_value, name)
        return None

    def _runtime_value(self, value: object) -> ResolvedValue:
        if type(value) in _PRIMITIVES:
            return value
        if isinstance(value, (types.FunctionType, types.MethodType, functools.partial)):
            function, owner = _unwrap(value)
            return (
                self._python_function(
                    function, RuntimeValue(owner) if owner is not None else None
                )
                if function
                else None
            )
        if isinstance(value, type):
            return self._runtime_class(value)
        if isinstance(value, types.ModuleType):
            path = self._filename(_stored(value, "__file__"))
            return (
                ModuleValue(path) if path is not None and self._inside(path) else None
            )
        return RuntimeValue(value)

    def _runtime_attribute(self, runtime: RuntimeValue, name: str) -> ResolvedValue:
        key = id(runtime.value), name
        if key not in self.runtime_dependencies:
            try:
                self.runtime_dependencies[key] = (
                    runtime.value,
                    name,
                    runtime_stamp(runtime.value, name),
                )
            except (ValueError, RuntimeError):
                self.cacheable = False
        stored = _stored(runtime.value, name, _MISSING)
        if stored is not _MISSING:
            return self._runtime_value(stored)
        for cls in _mro(type(runtime.value)):
            namespace = _class_dict(cls)
            if name not in namespace:
                continue
            item = namespace[name]
            if isinstance(item, types.MemberDescriptorType):
                # Python __slots__ stores plain data in this builtin descriptor.
                # Read it directly, bypassing plugin attribute hooks/properties.
                try:
                    return self._runtime_value(
                        item.__get__(runtime.value, type(runtime.value))
                    )
                except AttributeError:
                    return None
            if isinstance(item, types.FunctionType):
                return self._python_function(item, runtime)
            # Never bind/call descriptors, including property and __getattr__.
            # A plain class constant is a stored value, not an attribute getter.
            if type(item) in _PRIMITIVES:
                return item
            return None
        static_cls = self._runtime_class(runtime.value)
        return (
            self._instance_attribute(InstanceValue(static_cls), name)
            if static_cls
            else None
        )

    def attribute(self, value: ResolvedValue, name: str) -> ResolvedValue:
        if isinstance(value, RuntimeValue):
            key = "runtime", id(value.value), name
        elif isinstance(value, (ModuleValue, ClassValue, InstanceValue)):
            key = type(value), value, name
        else:
            return None
        if key in self._resolved_attributes:
            return self._resolved_attributes[key]
        result = self._attribute(value, name)
        self._resolved_attributes[key] = result
        return result

    def _attribute(self, value: ResolvedValue, name: str) -> ResolvedValue:
        try:
            if self.root is None or type(name) is not str or not name.isidentifier():
                return None
            if name.startswith("__"):
                return None
            if isinstance(value, ModuleValue):
                path = value.path.resolve(strict=True)
                if not self._inside(path):
                    return None
                return self._module_attribute(value, name)
            if isinstance(value, ClassValue):
                return self._class_attribute(value, name)
            if isinstance(value, InstanceValue):
                return self._instance_attribute(value, name)
            if isinstance(value, RuntimeValue):
                return self._runtime_attribute(value, name)
            return None
        except Exception as exc:
            self._issue(f"attribute lookup failed ({type(exc).__name__})")
            return None

    def construct(self, cls: ClassValue) -> InstanceValue:
        """Return an inert type reference; never run __init__ or other code."""
        return InstanceValue(cls)

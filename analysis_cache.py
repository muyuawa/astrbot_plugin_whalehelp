"""Bounded analysis results, validated against observed source and runtime data."""

from collections import OrderedDict
from concurrent.futures import Future
from dataclasses import dataclass
from re import Pattern
from threading import RLock

from .registration import command_roots
from .source_index import _stored, file_stamp, freeze_value, runtime_stamp


def signature(plugin, handler):
    regexes = []
    for item in _stored(handler, "event_filters", ()):
        if any(cls.__name__ == "RegexFilter" for cls in type(item).__mro__):
            pattern = _stored(item, "regex_str")
            if pattern is None:
                compiled = _stored(item, "regex")
                pattern = compiled.pattern if isinstance(compiled, Pattern) else None
            regexes.append(freeze_value(pattern))
    return (
        id(_stored(handler, "handler", handler)),
        id(_stored(plugin, "star_cls")),
        id(_stored(plugin, "module")),
        freeze_value(_stored(_stored(plugin, "module"), "__file__")),
        freeze_value(_stored(plugin, "name")),
        freeze_value(_stored(plugin, "display_name")),
        freeze_value(_stored(plugin, "module_path")),
        tuple(regexes),
        command_roots(handler),
    )


@dataclass
class Record:
    plugin: object
    handler: object
    signature: tuple
    result: object
    context: tuple | None
    files: tuple
    runtime: tuple

    def matches_context(self, context):
        if self.context is None or context is None:
            return self.context is context
        current = dict(context)
        return all(current.get(name) == value for name, value in self.context)

    def valid(self, current_signature):
        try:
            return (
                self.signature == current_signature
                and all(file_stamp(path) == stamp for path, stamp in self.files)
                and all(
                    runtime_stamp(owner, name) == stamp
                    for owner, name, stamp in self.runtime
                )
            )
        except (OSError, ValueError, RuntimeError):
            return False


class AnalysisCache:
    def __init__(self, limit=128):
        self._limit = limit
        self._records = OrderedDict()
        self._pending = {}
        self._lock = RLock()
        self._revision = 0
        self.content_revision = 0

    def evaluate(self, plugin, handler, context, compute):
        key = id(plugin), id(handler), context
        current = signature(plugin, handler)
        with self._lock:
            candidates = [
                (other_key, record)
                for other_key, record in reversed(self._records.items())
                if other_key[:2] == key[:2]
            ]
            revision = self._revision
        for other_key, record in candidates:
            if record.matches_context(context) and record.valid(current):
                with self._lock:
                    if other_key in self._records:
                        self._records.move_to_end(other_key)
                return record.result
        # Only identical overlapping requests wait for each other. The cache
        # lock is never held during source IO/analysis or dependency checks.
        pending_key = key, current, revision
        with self._lock:
            future = self._pending.get(pending_key)
            owner = future is None
            if owner:
                future = self._pending[pending_key] = Future()
        if not owner:
            return future.result()
        try:
            result, index = compute()
            record = Record(
                plugin,
                handler,
                current,
                result,
                tuple(index.context_reads.items()) if context is not None else None,
                tuple(index.watched_files.items()),
                tuple(index.runtime_dependencies.values()),
            )
            if index.cacheable and record.valid(signature(plugin, handler)):
                with self._lock:
                    if revision == self._revision:
                        previous = self._records.get(key)
                        self._records[key] = record
                        self._records.move_to_end(key)
                        if (
                            previous.result.entries if previous else ()
                        ) != result.entries:
                            self.content_revision += 1
                        while len(self._records) > self._limit:
                            self._records.popitem(last=False)
            future.set_result(result)
            return result
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            with self._lock:
                self._pending.pop(pending_key, None)

    def peek(self, plugin, handler):
        """Reuse an already checked context for dropdown labels; never scan."""
        current = signature(plugin, handler)
        with self._lock:
            records = [
                record
                for key, record in reversed(self._records.items())
                if key[:2] == (id(plugin), id(handler))
            ]
        for record in records:
            if record.result.context_allowed and record.valid(current):
                return record.result
        return None

    def invalidate(self, plugin=None):
        module = _stored(plugin, "module_path")
        with self._lock:
            self._revision += 1
            self.content_revision += 1
            if plugin is None:
                self._records.clear()
            else:
                for key, record in list(self._records.items()):
                    if record.plugin is plugin or (
                        module and _stored(record.plugin, "module_path") == module
                    ):
                        self._records.pop(key)

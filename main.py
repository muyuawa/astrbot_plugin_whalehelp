"""AstrBot entrypoint for the whale help menu."""

import asyncio
import re
from contextlib import suppress
from copy import deepcopy
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Node, Nodes, Plain
from astrbot.api.star import Context, Star
from astrbot.core.star.star_handler import star_handlers_registry
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .catalog import (
    collect_commands,
    command_names,
    menu_pages,
    text_menu,
)
from .configuration import apply_choices, collect_choices
from .discovery import discovery_revision, invalidate_discovery
from .renderer import MenuRenderer

try:
    from astrbot.core.star.session_plugin_manager import SessionPluginManager
except ModuleNotFoundError as exc:
    # This separate per-chat switch is absent in earlier AstrBot 4.x versions.
    if exc.name != "astrbot.core.star.session_plugin_manager":
        raise
    SessionPluginManager = None


MENU_NAMES = (
    "菜单",
    "帮助",
    "帮助菜单",
    "鲸鱼帮助",
    "菜单帮助",
    "帮助列表",
    "使用帮助",
    "功能菜单",
    "功能列表",
    "指令",
    "指令列表",
    "指令菜单",
    "help",
    "Help",
    "HELP",
    "menu",
    "Menu",
    "MENU",
    "commands",
)


def _plain_parts(text, limit=1800):
    """Keep URLs whole when they fit within the plain-text message limit."""
    links = [
        link
        for link in re.finditer(r"https?://\S+", text)
        if link.end() - link.start() <= limit
    ]
    start = 0
    while start < len(text):
        end = min(start + limit, len(text))
        for link in links:
            if link.start() < end < link.end():
                end = link.start() if link.start() > start else link.end()
                break
        yield text[start:end]
        start = end


BARE_MENU_PATTERN = (
    r"^\s*(?:" + "|".join(re.escape(name) for name in MENU_NAMES) + r")(?:\s+.*)?\s*$"
)


def _bare_filter():
    decorator = getattr(filter, "regex", None)
    return decorator(BARE_MENU_PATTERN, priority=109) if decorator else lambda fn: fn


def _finish(event):
    stop = getattr(event, "stop_event", None)
    if callable(stop):
        stop()


def _request(event, handlers, prefix, fallback):
    """Recover the entire query; AstrBot's str argument consumes one token only."""
    names = list(MENU_NAMES)
    trigger = "菜单"
    for handler in handlers:
        if (
            handler.handler_module_path == __name__
            and getattr(handler, "handler_name", "") == "menu"
        ):
            for item in handler.event_filters:
                if hasattr(item, "command_name"):
                    live = command_names(item)
                    if live:
                        trigger = live[0]
                        names.extend(live)
    message = event.get_message_str().strip()
    prefixes = "|".join(re.escape(p) for p in dict.fromkeys([prefix, "/"]) if p)
    matches = []
    for name in set(names):
        pattern = rf"(?:^|\s)(?:(?:{prefixes}))?{re.escape(name)}(?=\s|$)"
        match = re.search(pattern, message)
        if match:
            matches.append((match.start(), -len(name), match.end()))
    if matches:
        return trigger, message[min(matches)[2] :].strip()[:256]
    return trigger, str(fallback)[:256]


def _page_size(value):
    try:
        return max(6, min(24, int(value)))
    except (TypeError, ValueError):
        return 12


def _hook(name):
    """Register newer lifecycle hooks only when the installed AstrBot has them."""
    decorator = getattr(filter, name, None)
    return decorator() if decorator else lambda fn: fn


class WhaleHelp(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        root = (
            Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_whalehelp"
        )
        self.renderer = MenuRenderer(root / "menus")
        self._render_lock = asyncio.Lock()
        self._choices_task = None
        self._choices_generation = 0
        self._choices_snapshot = None
        self._choices_options = None
        self._choices_content_revision = None
        self._terminated = False

    def _choice_options(self):
        return deepcopy(
            {
                "include_builtin": self.config.get("include_builtin", False),
                "auto_discover": self.config.get("auto_discover", True),
                "extra_commands": self.config.get("extra_commands", []),
            }
        )

    def _schedule_choices_refresh(self, excluding=None):
        """Snapshot registries here; never scan source in a load/unload hook."""
        if self._terminated or not isinstance(
            getattr(self.config, "schema", None), dict
        ):
            return
        plugins = [p for p in self.context.get_all_stars() if p is not excluding]
        self._choices_options = self._choice_options()
        self._choices_content_revision = discovery_revision()
        self._choices_snapshot = (
            plugins,
            list(star_handlers_registry),
            self._choices_options,
        )
        self._choices_generation += 1
        if self._choices_task is None or self._choices_task.done():
            self._choices_task = asyncio.create_task(self._refresh_choices())

    async def _refresh_choices(self):
        try:
            while True:
                generation = self._choices_generation
                # Coalesce startup/install bursts into one scan after a quiet period.
                await asyncio.sleep(0.3)
                if generation != self._choices_generation:
                    continue
                choices = await asyncio.to_thread(
                    collect_choices, *self._choices_snapshot, cached_only=True
                )
                if generation != self._choices_generation:
                    continue
                apply_choices(self.config, choices)
                return
        except Exception as exc:
            logger.warning(
                f"WhaleHelp: 后台更新帮助配置选项失败 ({type(exc).__name__})"
            )

    async def initialize(self):
        self._schedule_choices_refresh()

    async def terminate(self):
        """A cancelled worker only owns private results; it cannot publish later."""
        self._terminated = True
        invalidate_discovery()
        if self._choices_task is not None:
            self._choices_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._choices_task

    @_hook("on_astrbot_loaded")
    async def on_astrbot_loaded(self):
        self._schedule_choices_refresh()

    @_hook("on_plugin_loaded")
    async def on_plugin_loaded(self, metadata):
        invalidate_discovery(metadata)
        self._schedule_choices_refresh()

    @_hook("on_plugin_unloaded")
    async def on_plugin_unloaded(self, metadata):
        invalidate_discovery(metadata)
        self._schedule_choices_refresh(excluding=metadata)

    @_bare_filter()
    async def bare_menu(self, event: AstrMessageEvent):
        """直接发送帮助或菜单也能查看功能，不匹配句子中间的帮助二字。"""
        if not self.config.get("respond_to_bare_help", True):
            return
        if not re.fullmatch(BARE_MENU_PATTERN, event.get_message_str()):
            return
        async for result in self.menu(event):
            yield result

    @filter.command("菜单", alias=set(MENU_NAMES) - {"菜单"}, priority=110)
    async def menu(self, event: AstrMessageEvent, query: str = ""):
        """查看全部插件指令；帮助、help 等入口支持页码、关键词和文字版。"""
        quiet = getattr(event, "should_call_llm", None)
        if callable(quiet):
            quiet(False)
        bot_name = str(self.config.get("bot_name", "AstrBot")).strip() or "AstrBot"
        try:
            cfg = self.context.get_config(event.unified_msg_origin)
            prefixes = cfg.get("wake_prefix", ["/"])
            if isinstance(prefixes, str):
                prefix = prefixes
            else:
                prefix = next((str(p) for p in prefixes if p), "")
            handlers = list(star_handlers_registry)
            if self._choice_options() != self._choices_options:
                self._schedule_choices_refresh()
            trigger, query = _request(event, handlers, prefix, query)
            if SessionPluginManager is not None:
                handlers = await SessionPluginManager.filter_handlers_by_session(
                    event, handlers
                )
            tokens = query.split()
            text_mode = bool(tokens and tokens[0] in ("文字", "text"))
            if text_mode:
                tokens.pop(0)
            plugins = list(self.context.get_all_stars())
            entries = await asyncio.to_thread(
                collect_commands, handlers, plugins, event, cfg, dict(self.config)
            )
            # Dropdowns can now reuse the analysis performed for this menu.
            if self._choices_content_revision != discovery_revision():
                self._schedule_choices_refresh()
            pages = menu_pages(
                entries,
                " ".join(tokens),
                page_size=_page_size(self.config.get("commands_per_page", 12)),
            )
            title = str(self.config.get("title", "")).strip() or f"{bot_name}的帮助菜单"
            extra_text = str(self.config.get("extra_text") or "").strip()
            explicit_page = bool(
                tokens and tokens[-1].isascii() and tokens[-1].isdigit()
            )
        except Exception as exc:
            logger.warning(f"WhaleHelp: 指令目录读取失败 ({type(exc).__name__})")
            yield event.plain_result(
                f"{bot_name}暂时无法读取指令，请稍后重试或查看 AstrBot 日志。"
            )
            _finish(event)
            return

        messages = []
        for index, page in enumerate(pages):
            text = text_menu(page, title=title, prefix=prefix, trigger=trigger)
            if text_mode:
                messages.append(("text", text))
                continue
            try:
                async with self._render_lock:
                    self.renderer.title = title
                    self.renderer.bot_name = bot_name
                    self.renderer.subtitle = str(
                        self.config.get("subtitle", "")
                    ).strip()
                    self.renderer.extra_text = (
                        extra_text if index == len(pages) - 1 else ""
                    )
                    backgrounds = self.config.get("background_image", [])
                    self.renderer.background = (
                        backgrounds[0]
                        if isinstance(backgrounds, list) and backgrounds
                        else ""
                    )
                    self.renderer.background_fade = self.config.get(
                        "background_fade", 60
                    )
                    path = await asyncio.to_thread(
                        self.renderer.render, page, prefix, trigger
                    )
                    # Keep bytes now: later pages may evict old cache files.
                    data = await asyncio.to_thread(Path(path).read_bytes)
                messages.append(("image", Image.fromBytes(data)))
            except Exception as exc:
                logger.warning(
                    f"WhaleHelp: 图片生成失败，使用文字菜单 ({type(exc).__name__})"
                )
                text = "图片暂时没准备好，先送上文字菜单。\n\n" + text
                messages.append(("text", text))

        if not explicit_page and event.get_platform_name() == "aiocqhttp":
            nodes = []
            for index, (page, (kind, value)) in enumerate(zip(pages, messages)):
                content = (
                    [Plain(value)]
                    if kind == "text"
                    else [
                        Plain(f"{title} · 第 {page.number}/{page.total_pages} 页"),
                        value,
                    ]
                )
                if extra_text and index == len(messages) - 1:
                    content.append(Plain(extra_text))
                nodes.append(
                    Node(uin=str(event.get_self_id()), name=bot_name, content=content)
                )
            try:
                # One Nodes wrapper sends one forward record, rather than one per page.
                await event.send(MessageChain([Nodes(nodes)]))
                _finish(event)
                return
            except Exception as exc:
                logger.warning(
                    f"WhaleHelp: 合并转发失败，逐页发送 ({type(exc).__name__})"
                )
        for kind, value in messages:
            if kind == "image":
                yield event.chain_result([value])
            else:
                for part in _plain_parts(value):
                    yield event.plain_result(part)
        if messages and extra_text:
            for part in _plain_parts(extra_text):
                yield event.plain_result(part)
        _finish(event)

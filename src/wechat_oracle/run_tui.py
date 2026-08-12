"""Textual dashboard used by `wechat-oracle run`.

The supervisor still owns the child processes. This module only renders their
status and log stream, so the production process topology stays:
`ingest live` + `dispatcher`.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
import threading
import time
from dataclasses import dataclass
from typing import Callable

from rich.cells import set_cell_size
from rich.markup import escape
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Input, Label, ListItem, ListView, RichLog, Static, TextArea

from .config import settings
from .config_store import AgentRuntimeConfig, load_agent_runtime_config
from .db import get_conn, transaction
from .log_utils import append_event
from .agent.memory import (
    get_group_memory,
    get_persona_drift,
    upsert_group_memory,
    upsert_persona_drift,
)
from .agent.local_ask import (
    LocalAskGroup,
    list_local_ask_groups,
    resolve_local_ask_group,
    run_local_ask,
)


@dataclass(frozen=True)
class MemoryEditResult:
    group_memory: str
    persona_drift: str


@dataclass
class BalanceStatus:
    label: str = "loading"
    updated_at: float = 0.0
    refreshing: bool = False


_BALANCE_STATUS = BalanceStatus()
_BALANCE_LOCK = threading.Lock()
_BALANCE_REFRESH_SECONDS = 30.0
_LABEL_STYLE = "bold #5ccfe6"
_OK_STYLE = "#8bdc7f"
_WARN_STYLE = "#f0c674"
_BAD_STYLE = "#ff6b6b"
_MUTED_STYLE = "dim"
_SEP = "[#245b73]｜[/]"


class GroupPickerScreen(ModalScreen[LocalAskGroup | None]):
    """Modal list used by the `g` shortcut."""

    BINDINGS = [
        ("escape", "cancel", "取消"),
    ]

    def __init__(self, groups: list[LocalAskGroup]) -> None:
        super().__init__()
        self._groups = groups

    def compose(self) -> ComposeResult:
        with Vertical(id="group-picker"):
            yield Static("选择群聊", id="group-picker-title")
            items: list[ListItem] = []
            for group in self._groups:
                item = ListItem(Label(_format_group_item(group)))
                item.group = group  # type: ignore[attr-defined]
                items.append(item)
            yield ListView(*items, id="group-picker-list")
            yield Static("回车选择，Esc 取消", id="group-picker-help")

    def on_mount(self) -> None:
        self.query_one("#group-picker-list", ListView).focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        group = getattr(event.item, "group", None)
        self.dismiss(group)

    def action_cancel(self) -> None:
        self.dismiss(None)


class AskScreen(ModalScreen[str | None]):
    """One-shot Local Ask input, opened by the `a` shortcut."""

    BINDINGS = [
        ("escape", "cancel", "取消"),
    ]

    def __init__(self, *, group: LocalAskGroup, allow_writes: bool) -> None:
        super().__init__()
        self._group = group
        self._allow_writes = allow_writes

    def compose(self) -> ComposeResult:
        mode = "允许写记忆" if self._allow_writes else "只读"
        with Vertical(id="ask-dialog"):
            yield Static("询问当前群", id="ask-dialog-title")
            yield Static(
                f"当前群：{self._group.short_label}　权限：{mode}",
                id="ask-dialog-meta",
            )
            yield Input(
                placeholder="输入问题，按回车提交",
                id="ask-dialog-input",
            )
            yield Static("回车提交，Esc 取消", id="ask-dialog-help")

    def on_mount(self) -> None:
        self.query_one("#ask-dialog-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.dismiss(event.value.strip() or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class MemoryTextArea(TextArea):
    """TextArea that lets the modal-level editing shortcuts win."""

    def on_key(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.key == "escape":
            event.stop()
            self.screen.action_cancel()
        elif event.key == "ctrl+s":
            event.stop()
            self.screen.action_save()
        elif event.key == "ctrl+t":
            event.stop()
            self.screen.action_toggle_doc()


class MemoryEditorScreen(ModalScreen[MemoryEditResult | None]):
    """Edit group_memory and persona_drift for the selected group."""

    BINDINGS = [
        ("ctrl+s", "save", "保存"),
        ("ctrl+t", "toggle_doc", "切换"),
        ("escape", "cancel", "取消"),
    ]

    def __init__(
        self,
        *,
        group: LocalAskGroup,
        group_memory: str,
        persona_drift: str,
    ) -> None:
        super().__init__()
        self._group = group
        self._texts = {
            "group_memory": group_memory,
            "persona_drift": persona_drift,
        }
        self._current = "group_memory"

    def compose(self) -> ComposeResult:
        with Vertical(id="memory-editor"):
            yield Static("", id="memory-editor-title")
            yield Static("", id="memory-editor-meta")
            yield MemoryTextArea(
                self._texts[self._current],
                soft_wrap=True,
                tab_behavior="indent",
                show_line_numbers=False,
                id="memory-editor-text",
            )
            yield Static("", id="memory-editor-help")

    def on_mount(self) -> None:
        self._refresh_header()
        self.query_one("#memory-editor-text", MemoryTextArea).focus()

    def action_toggle_doc(self) -> None:
        self._store_current_text()
        self._current = (
            "persona_drift" if self._current == "group_memory" else "group_memory"
        )
        text_area = self.query_one("#memory-editor-text", MemoryTextArea)
        text_area.text = self._texts[self._current]
        self._refresh_header()

    def action_save(self) -> None:
        self._store_current_text()
        self.dismiss(
            MemoryEditResult(
                group_memory=self._texts["group_memory"],
                persona_drift=self._texts["persona_drift"],
            )
        )

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _store_current_text(self) -> None:
        self._texts[self._current] = self.query_one("#memory-editor-text", MemoryTextArea).text

    def _refresh_header(self) -> None:
        title = self.query_one("#memory-editor-title", Static)
        meta = self.query_one("#memory-editor-meta", Static)
        help_text = self.query_one("#memory-editor-help", Static)
        doc_label = (
            "群记忆（group_memory）" if self._current == "group_memory" else "人格漂移（persona_drift）"
        )
        cap = settings.agent_memory_max_chars if self._current == "group_memory" else 4000
        current_len = len(self._texts[self._current])
        title.update(f"记忆编辑：{doc_label}")
        meta.update(
            f"当前群：{self._group.short_label}　字数：{current_len}/{cap}　"
            f"ID：{_clip(self._group.group_id, 36)}"
        )
        help_text.update(
            "Ctrl+T 切换文档，Ctrl+S 保存两份内容，Esc 取消"
        )


class MemberKnowledgeConsentScreen(ModalScreen[bool]):
    """Explicit consent gate before archived chat is sent to the profile LLM."""

    BINDINGS = [("escape", "cancel", "取消")]

    def __init__(self, *, message_count: int, member_count: int, estimated_calls: int) -> None:
        super().__init__()
        self._message_count = message_count
        self._member_count = member_count
        self._estimated_calls = estimated_calls

    def compose(self) -> ComposeResult:
        with Vertical(id="config-value-editor"):
            yield Static("启用成员知识库", id="config-value-title")
            yield Static(
                "启用后会把已选群的历史发言和派生画像发送给当前配置的 "
                "OpenAI-compatible API。模型可推断敏感属性，这些画像可能用于群回复和总结。\n\n"
                f"待处理约 {self._message_count} 条消息、{self._member_count} 位成员，"
                f"预计至少 {self._estimated_calls} 次模型调用。",
                id="config-value-help",
            )
            with Horizontal(id="config-value-buttons"):
                yield Button("我理解并启用", id="member-kb-consent-confirm", variant="error")
                yield Button("取消", id="member-kb-consent-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "member-kb-consent-confirm":
            self.dismiss(True)
        else:
            self.dismiss(False)

    def action_cancel(self) -> None:
        self.dismiss(False)


@dataclass(frozen=True)
class MemberSectionEditResult:
    section: str
    content: str
    locked: bool


class MemberSectionEditorScreen(ModalScreen[MemberSectionEditResult | None]):
    BINDINGS = [("ctrl+s", "save", "保存"), ("escape", "cancel", "取消")]

    def __init__(self, *, section: str, content: str, locked: bool) -> None:
        super().__init__()
        self._section = section
        self._locked = locked
        self._content = content

    def compose(self) -> ComposeResult:
        with Vertical(id="memory-editor"):
            yield Static(f"编辑成员画像栏目：{self._section}", id="memory-editor-title")
            yield Static(
                "手工锁定后，定时画像模型不能覆盖此栏目。",
                id="memory-editor-meta",
            )
            yield MemoryTextArea(self._content, id="member-section-text")
            with Horizontal(id="memory-editor-actions"):
                yield Button(
                    f"锁定：{'是' if self._locked else '否'}",
                    id="member-section-lock",
                )
                yield Button("保存", id="member-section-save", variant="primary")
                yield Button("取消", id="member-section-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "member-section-lock":
            self._locked = not self._locked
            event.button.label = f"锁定：{'是' if self._locked else '否'}"
        elif event.button.id == "member-section-save":
            self.action_save()
        else:
            self.action_cancel()

    def action_save(self) -> None:
        self.dismiss(
            MemberSectionEditResult(
                section=self._section,
                content=self.query_one("#member-section-text", TextArea).text,
                locked=self._locked,
            )
        )

    def action_cancel(self) -> None:
        self.dismiss(None)


class MemberKnowledgeActionConfirmScreen(ModalScreen[bool]):
    def __init__(self, *, title: str, body: str) -> None:
        super().__init__()
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        with Vertical(id="config-value-editor"):
            yield Static(self._title, id="config-value-title")
            yield Static(self._body, id="config-value-help")
            with Horizontal(id="config-value-buttons"):
                yield Button("确认", id="member-action-confirm", variant="error")
                yield Button("取消", id="member-action-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "member-action-confirm")


class MemberKnowledgeScreen(ModalScreen[None]):
    """Local browser/editor for one group's evidence-linked member profiles."""

    BINDINGS = [("escape", "close", "关闭")]

    def __init__(self, group: LocalAskGroup) -> None:
        super().__init__()
        self._group = group
        self._members: list[dict] = []
        self._selected: dict | None = None
        self._profile: dict = {}
        self._section_index = 0
        self._sections = (
            "identity", "interests", "skills", "communication_style", "habits",
            "relationships", "opinions", "sensitive_inferences", "recent_focus",
        )

    def compose(self) -> ComposeResult:
        with Vertical(id="memory-editor"):
            yield Static(f"成员知识库 · {self._group.label}", id="memory-editor-title")
            yield Static(
                "⚠ 群聊原文与画像会发送给配置的模型 API；敏感推断可能进入群回复和总结。",
                id="memory-editor-meta",
            )
            with Horizontal():
                yield ListView(id="member-kb-members")
                yield RichLog(id="member-kb-detail", wrap=True, highlight=False, markup=False)
            with Horizontal(id="memory-editor-actions"):
                yield Button("编辑栏目", id="member-kb-edit")
                yield Button("切换栏目", id="member-kb-next-section")
                yield Button("删除画像", id="member-kb-delete", variant="error")
                yield Button("完整重建", id="member-kb-rebuild")
                yield Button("刷新", id="member-kb-refresh")
                yield Button("关闭", id="member-kb-close")

    def on_mount(self) -> None:
        self._reload_members()

    def _reload_members(self) -> None:
        from .member_knowledge import list_member_profiles

        try:
            with get_conn() as conn:
                self._members = list_member_profiles(conn, self._group.group_id)
        except Exception as exc:
            self.query_one("#member-kb-detail", RichLog).write(
                f"读取成员知识库失败：{type(exc).__name__}: {exc}"
            )
            return
        view = self.query_one("#member-kb-members", ListView)
        view.clear()
        for index, item in enumerate(self._members):
            name = item.get("display_name") or item.get("current_display_name") or item.get("sender_wxid") or "未知成员"
            count = item.get("message_count", 0)
            view.append(ListItem(Label(f"{name}  ·  {count} 条"), id=f"member-kb-row-{index}"))
        if self._members:
            self._select_member(0)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item_id = event.item.id or ""
        if item_id.startswith("member-kb-row-"):
            self._select_member(int(item_id.rsplit("-", 1)[1]))

    def _select_member(self, index: int) -> None:
        if not 0 <= index < len(self._members):
            return
        from .member_knowledge import get_member_profile, list_member_messages

        self._selected = self._members[index]
        sender = str(self._selected.get("sender_wxid") or "")
        try:
            with get_conn() as conn:
                self._profile = get_member_profile(conn, self._group.group_id, sender) or {}
                messages = list_member_messages(
                    conn, self._group.group_id, sender, limit=50
                )
        except Exception as exc:
            self.query_one("#member-kb-detail", RichLog).write(
                f"读取画像失败：{type(exc).__name__}: {exc}"
            )
            return
        self._render_detail(messages)

    def _profile_sections(self) -> dict:
        value = self._profile.get("profile") or self._profile.get("profile_json") or {}
        if isinstance(value, str):
            import json as _json
            try:
                value = _json.loads(value)
            except Exception:
                value = {}
        return value if isinstance(value, dict) else {}

    def _render_detail(self, messages: list[dict]) -> None:
        import json as _json

        log = self.query_one("#member-kb-detail", RichLog)
        log.clear()
        aliases = self._profile.get("aliases") or []
        locked = set(self._profile.get("locked_sections") or [])
        log.write(f"成员：{self._profile.get('display_name') or self._profile.get('current_display_name') or self._profile.get('sender_wxid')}")
        log.write(f"wxid：{self._profile.get('sender_wxid')}  昵称历史：{', '.join(map(str, aliases)) or '-'}")
        log.write(f"总画像：{self._profile.get('summary_text') or '(尚未生成)'}")
        log.write("\n结构化画像：")
        for section in self._sections:
            marker = " [已锁定]" if section in locked else ""
            value = self._profile_sections().get(section, "")
            log.write(f"- {section}{marker}: {value or '-'}")
        log.write("\n结论与证据：")
        for claim in self._profile.get("claims") or []:
            text = claim.get("claim_text") or claim.get("text") or ""
            evidence = claim.get("evidence_msg_ids") or claim.get("evidence") or []
            log.write(
                f"- [{claim.get('status','current')}] {text} · {claim.get('basis','?')} "
                f"· confidence={claim.get('confidence','?')} · sensitive={bool(claim.get('sensitive'))} "
                f"· evidence={_json.dumps(evidence, ensure_ascii=False)}"
            )
        log.write("\n最近原始消息（本地读取）：")
        for message in messages:
            body = message.get("content_text") or message.get("transcript") or ""
            log.write(f"#{message.get('msg_id')} {message.get('t')}  {body}")

    def _write_detail(self, text: str) -> None:
        self.query_one("#member-kb-detail", RichLog).write(text)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        match event.button.id:
            case "member-kb-close":
                self.dismiss(None)
            case "member-kb-refresh":
                self._reload_members()
            case "member-kb-next-section":
                self._section_index = (self._section_index + 1) % len(self._sections)
                event.button.label = f"栏目：{self._sections[self._section_index]}"
            case "member-kb-edit":
                self._edit_section()
            case "member-kb-delete":
                if self._selected:
                    self.app.push_screen(
                        MemberKnowledgeActionConfirmScreen(
                            title="删除派生画像",
                            body="只删除画像、结论和更新游标；原始群聊消息永久保留。",
                        ),
                        self._on_delete_confirmed,
                    )
            case "member-kb-rebuild":
                if self._selected:
                    self.app.push_screen(
                        MemberKnowledgeActionConfirmScreen(
                            title="完整历史重建",
                            body="将清除派生画像并重新把该成员全部历史发言发送给模型 API。",
                        ),
                        self._on_rebuild_confirmed,
                    )

    def _edit_section(self) -> None:
        if not self._selected:
            return
        section = self._sections[self._section_index]
        self.app.push_screen(
            MemberSectionEditorScreen(
                section=section,
                content=str(self._profile_sections().get(section) or ""),
                locked=section in set(self._profile.get("locked_sections") or []),
            ),
            self._on_section_edited,
        )

    def _on_section_edited(self, result: MemberSectionEditResult | None) -> None:
        if result is None or not self._selected:
            return
        from .member_knowledge import update_member_profile_section

        sender = str(self._selected.get("sender_wxid") or "")
        with get_conn() as conn:
            update_member_profile_section(
                conn, self._group.group_id, sender,
                result.section, result.content, locked=result.locked,
            )
        self._reload_members()

    def _on_delete_confirmed(self, accepted: bool) -> None:
        if not accepted or not self._selected:
            return
        from .member_knowledge import delete_member_profile

        sender = str(self._selected.get("sender_wxid") or "")
        with get_conn() as conn:
            delete_member_profile(conn, self._group.group_id, sender, keep_messages=True)
        self._reload_members()

    def _on_rebuild_confirmed(self, accepted: bool) -> None:
        if not accepted or not self._selected:
            return
        sender = str(self._selected.get("sender_wxid") or "")
        self.query_one("#member-kb-detail", RichLog).write("重建任务已启动……")

        def worker() -> None:
            from .llm import build_llm_client
            from .member_knowledge import reset_member_profile, run_member_update

            try:
                with get_conn() as conn:
                    reset_member_profile(conn, self._group.group_id, sender)
                    llm = build_llm_client(
                        provider=settings.llm_provider,
                        api_key=settings.llm_api_key,
                        endpoint=settings.llm_endpoint,
                        json_mode=settings.llm_json_mode,
                    )
                    run_member_update(
                        conn, self._group.group_id, sender, llm,
                        chunk_chars=settings.member_kb_chunk_chars,
                        retries=settings.member_kb_retries,
                    )
                self.app.call_from_thread(self._reload_members)
            except Exception as exc:
                self.app.call_from_thread(
                    self._write_detail,
                    f"重建失败：{type(exc).__name__}",
                )

        threading.Thread(target=worker, daemon=True, name="member-kb-rebuild").start()

    def action_close(self) -> None:
        self.dismiss(None)


class ConfigValueScreen(ModalScreen[str | None]):
    """Edit one config value in a small, focused dialog."""

    BINDINGS = [
        ("escape", "cancel", "取消"),
    ]

    def __init__(self, *, title: str, value: str, password: bool = False) -> None:
        super().__init__()
        self._title = title
        self._value = value
        self._password = password

    def compose(self) -> ComposeResult:
        with Vertical(id="config-value-editor"):
            yield Static(self._title, id="config-value-title")
            yield Input(value=self._value, password=self._password, id="config-value-input")
            with Horizontal(id="config-value-buttons"):
                yield Button("确定", id="config-value-save", variant="primary")
                yield Button("取消", id="config-value-cancel")
            yield Static("输入完成后按回车或点击确定；Esc 取消", id="config-value-help")

    def on_mount(self) -> None:
        self.query_one("#config-value-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._save()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "config-value-save":
            self._save()
        elif event.button.id == "config-value-cancel":
            self.action_cancel()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _save(self) -> None:
        self.dismiss(self.query_one("#config-value-input", Input).value.strip())


class ConfigGroupSelectionScreen(ModalScreen[tuple[str, ...] | None]):
    """Select exact canonical groups from the local authorization table."""

    BINDINGS = [("escape", "cancel", "取消")]

    def __init__(
        self,
        options: tuple[tuple[str, str], ...],
        selected: tuple[str, ...],
    ) -> None:
        super().__init__()
        self._options = options
        self._selected = set(selected)

    def compose(self) -> ComposeResult:
        with Vertical(id="config-value-editor"):
            yield Static("选择自动归档与总结的群", id="config-value-title")
            if not self._options:
                yield Static(
                    "还没有已授权群。先运行 WeChatOracle.exe raw groups，"
                    "再用 raw authorize <canonical-id> 授权。",
                    id="config-value-help",
                )
            for index, (group_id, name) in enumerate(self._options):
                marker = "☑" if group_id in self._selected else "☐"
                yield Button(
                    f"{marker} {name}  {_clip(group_id, 32)}",
                    id=f"config-group-{index}",
                    classes="config-menu-item",
                    compact=True,
                )
            with Horizontal(id="config-value-buttons"):
                yield Button("确定", id="config-groups-save", variant="primary")
                yield Button("取消", id="config-groups-cancel")

    def on_mount(self) -> None:
        controls = self._controls()
        if controls:
            controls[0].focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        button_id = event.button.id or ""
        if button_id.startswith("config-group-"):
            index = int(button_id.removeprefix("config-group-"))
            group_id, name = self._options[index]
            if group_id in self._selected:
                self._selected.remove(group_id)
                marker = "☐"
            else:
                self._selected.add(group_id)
                marker = "☑"
            event.button.label = f"{marker} {name}  {_clip(group_id, 32)}"
        elif button_id == "config-groups-save":
            ordered = tuple(group_id for group_id, _ in self._options if group_id in self._selected)
            self.dismiss(ordered)
        elif button_id == "config-groups-cancel":
            self.action_cancel()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _controls(self) -> list[Button]:
        return [
            *[self.query_one(f"#config-group-{index}", Button) for index in range(len(self._options))],
            self.query_one("#config-groups-save", Button),
            self.query_one("#config-groups-cancel", Button),
        ]


class ConfigBackendScreen(ModalScreen[str | None]):
    """Pick native/openclaw/pi in a dedicated option dialog."""

    BINDINGS = [
        ("escape", "cancel", "取消"),
    ]

    def __init__(self, config: AgentRuntimeConfig, current: str) -> None:
        super().__init__()
        self._config = config
        self._current = current

    def compose(self) -> ComposeResult:
        native_state = "已配置" if self._config.native_configured else "不可用：缺少 WO_LLM_API_KEY"
        openclaw_state = "已配置" if self._config.openclaw_token_configured else "不可用：缺少 WO_OPENCLAW_TOKEN"
        pi_state = "已配置" if self._config.pi_configured else "不可用：找不到 Pi CLI"
        with Vertical(id="config-backend-picker"):
            yield Static("选择 Agent 后端", id="config-backend-title")
            yield Button(
                f"{_current_marker(self._current, 'native')}Native：本进程工具链（{native_state}）",
                id="config-backend-native",
                classes="config-menu-item",
                disabled=not self._config.native_configured,
                compact=True,
            )
            yield Button(
                f"{_current_marker(self._current, 'openclaw')}OpenClaw：外部 Agent runtime（{openclaw_state}）",
                id="config-backend-openclaw",
                classes="config-menu-item",
                disabled=not self._config.openclaw_token_configured,
                compact=True,
            )
            yield Button(
                f"{_current_marker(self._current, 'pi')}Pi：本机隔离 RPC（{pi_state}）",
                id="config-backend-pi",
                classes="config-menu-item",
                disabled=not self._config.pi_configured,
                compact=True,
            )
            with Horizontal(id="config-backend-buttons"):
                yield Button("取消", id="config-cancel", compact=True)
            yield Static("点击一个选项，或用 Tab/方向键切换后按回车；Esc 取消", id="config-backend-help")

    def on_mount(self) -> None:
        target_id = {
            "openclaw": "#config-backend-openclaw",
            "pi": "#config-backend-pi",
        }.get(self._current, "#config-backend-native")
        try:
            target = self.query_one(target_id, Button)
            if not target.disabled:
                target.focus()
                return
        except NoMatches:
            pass
        self._focus_first_enabled()

    def on_key(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.key == "down":
            event.stop()
            self._focus_next()
        elif event.key == "up":
            event.stop()
            self._focus_previous()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "config-backend-native":
            self.dismiss("native")
        elif event.button.id == "config-backend-openclaw":
            self.dismiss("openclaw")
        elif event.button.id == "config-backend-pi":
            self.dismiss("pi")
        elif event.button.id == "config-cancel":
            self.action_cancel()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _focus_first_enabled(self) -> None:
        for control in self._focus_controls():
            if isinstance(control, Button) and not control.disabled:
                control.focus()
                return

    def _focus_controls(self) -> list[Button]:
        return [
            self.query_one("#config-backend-native", Button),
            self.query_one("#config-backend-openclaw", Button),
            self.query_one("#config-cancel", Button),
        ]

    def _focus_next(self) -> None:
        controls = self._focus_controls()
        current = self.focused
        if current in controls:
            start = controls.index(current)
            for offset in range(1, len(controls) + 1):
                candidate = controls[(start + offset) % len(controls)]
                if not candidate.disabled:
                    candidate.focus()
                    return

    def _focus_previous(self) -> None:
        controls = self._focus_controls()
        current = self.focused
        if current in controls:
            start = controls.index(current)
            for offset in range(1, len(controls) + 1):
                candidate = controls[(start - offset) % len(controls)]
                if not candidate.disabled:
                    candidate.focus()
                    return


class ConfigProactiveModeScreen(ModalScreen[str | None]):
    """Pick how probability wakeups may participate in group chat."""

    BINDINGS = [
        ("escape", "cancel", "取消"),
    ]

    _OPTIONS = (
        ("off", "Off：只响应 @ 和引用回复"),
        ("reactive", "Reactive：偶尔接当前话题，不主动开新话题"),
        ("proactive", "Proactive：可基于上下文主动问一句或牵一条旧线"),
    )

    def __init__(self, current: str) -> None:
        super().__init__()
        self._current = current if current in {"off", "reactive", "proactive"} else "reactive"

    def compose(self) -> ComposeResult:
        with Vertical(id="config-proactive-picker"):
            yield Static("选择主动模式", id="config-proactive-title")
            for mode, label in self._OPTIONS:
                yield Button(
                    f"{_current_marker(self._current, mode)}{label}",
                    id=f"config-proactive-{mode}",
                    classes="config-menu-item",
                    compact=True,
                )
            with Horizontal(id="config-proactive-buttons"):
                yield Button("取消", id="config-proactive-cancel", compact=True)
            yield Static(
                "Off 会关闭 probability；Reactive 只接话；Proactive 允许低频主动抛话题。Esc 取消",
                id="config-proactive-help",
            )

    def on_mount(self) -> None:
        self.query_one(f"#config-proactive-{self._current}", Button).focus()

    def on_key(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.key == "down":
            event.stop()
            self._focus_next()
        elif event.key == "up":
            event.stop()
            self._focus_previous()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        button_id = event.button.id or ""
        if button_id.startswith("config-proactive-") and button_id != "config-proactive-cancel":
            self.dismiss(button_id.removeprefix("config-proactive-"))
        elif button_id == "config-proactive-cancel":
            self.action_cancel()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _focus_controls(self) -> list[Button]:
        return [
            self.query_one("#config-proactive-off", Button),
            self.query_one("#config-proactive-reactive", Button),
            self.query_one("#config-proactive-proactive", Button),
            self.query_one("#config-proactive-cancel", Button),
        ]

    def _focus_next(self) -> None:
        controls = self._focus_controls()
        current = self.focused
        if current in controls:
            controls[(controls.index(current) + 1) % len(controls)].focus()

    def _focus_previous(self) -> None:
        controls = self._focus_controls()
        current = self.focused
        if current in controls:
            controls[(controls.index(current) - 1) % len(controls)].focus()


class ConfigMentionPolicyScreen(ModalScreen[str | None]):
    """Pick whether outgoing group replies should @ the requester."""

    BINDINGS = [
        ("escape", "cancel", "取消"),
    ]

    _OPTIONS = (
        ("always", "Always：所有群回复都 @ 触发者"),
        ("explicit", "Explicit：只在 @ / 引用 / 命令这类显式触发时 @"),
        ("never", "Never：群回复都不 @ 人"),
    )

    def __init__(self, current: str) -> None:
        super().__init__()
        self._current = current if current in {"always", "explicit", "never"} else "explicit"

    def compose(self) -> ComposeResult:
        with Vertical(id="config-mention-picker"):
            yield Static("选择 @ 策略", id="config-mention-title")
            for policy, label in self._OPTIONS:
                yield Button(
                    f"{_current_marker(self._current, policy)}{label}",
                    id=f"config-mention-{policy}",
                    classes="config-menu-item",
                    compact=True,
                )
            with Horizontal(id="config-mention-buttons"):
                yield Button("取消", id="config-mention-cancel", compact=True)
            yield Static(
                "Explicit 是默认值：显式问答会 @；probability / proactive 普通发送。Esc 取消",
                id="config-mention-help",
            )

    def on_mount(self) -> None:
        self.query_one(f"#config-mention-{self._current}", Button).focus()

    def on_key(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.key == "down":
            event.stop()
            self._focus_next()
        elif event.key == "up":
            event.stop()
            self._focus_previous()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        button_id = event.button.id or ""
        if button_id.startswith("config-mention-") and button_id != "config-mention-cancel":
            self.dismiss(button_id.removeprefix("config-mention-"))
        elif button_id == "config-mention-cancel":
            self.action_cancel()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _focus_controls(self) -> list[Button]:
        return [
            self.query_one("#config-mention-always", Button),
            self.query_one("#config-mention-explicit", Button),
            self.query_one("#config-mention-never", Button),
            self.query_one("#config-mention-cancel", Button),
        ]

    def _focus_next(self) -> None:
        controls = self._focus_controls()
        current = self.focused
        if current in controls:
            controls[(controls.index(current) + 1) % len(controls)].focus()

    def _focus_previous(self) -> None:
        controls = self._focus_controls()
        current = self.focused
        if current in controls:
            controls[(controls.index(current) - 1) % len(controls)].focus()


class ConfigScreen(ModalScreen[AgentRuntimeConfig | None]):
    """Menu-style editor for the agent runtime config written to `.env`."""

    BINDINGS = [
        ("ctrl+s", "save", "保存"),
        ("escape", "cancel", "取消"),
    ]

    def __init__(self, config: AgentRuntimeConfig) -> None:
        super().__init__()
        self._config = config
        self._backend = "native"
        self._proactive_mode = (
            config.proactive_mode
            if config.proactive_mode in {"off", "reactive", "proactive"}
            else "reactive"
        )
        self._agent_base_probability = _normalize_probability(config.agent_base_probability)
        self._reply_mention_policy = (
            config.reply_mention_policy
            if config.reply_mention_policy in {"always", "explicit", "never"}
            else "explicit"
        )
        self._continuation_enabled = bool(config.continuation_enabled)
        self._continuation_max_followups = int(config.continuation_max_followups)
        self._continuation_delay_seconds = int(config.continuation_delay_seconds)
        self._continuation_ttl_seconds = int(config.continuation_ttl_seconds)
        self._llm_model = config.llm_model
        self._llm_endpoint = config.llm_endpoint
        self._llm_api_key_update: str | None = None
        self._groups = tuple(config.groups)
        self._raw_wechat_enabled = bool(config.raw_wechat_enabled)
        self._raw_wechat_account = config.raw_wechat_account
        self._hourly_summary_enabled = bool(config.hourly_summary_enabled)
        self._daily_summary_enabled = bool(config.daily_summary_enabled)
        self._member_kb_enabled = bool(config.member_kb_enabled)
        self._openclaw_agent_id = config.openclaw_agent_id

    def compose(self) -> ComposeResult:
        native_state = "已配置" if self._config.native_configured else "缺少 WO_LLM_API_KEY"
        with Vertical(id="config-editor"):
            yield Static("运行配置", id="config-editor-title")
            yield Static(
                f"SQLite 本地记忆库 + OpenAI 兼容 API：{native_state}",
                id="config-editor-meta",
            )
            yield Static("改完后需要保存才会写入 .env，并重启调度进程。", id="config-editor-save-hint")
            yield Button("", id="config-menu-api-endpoint", classes="config-menu-item", compact=True)
            yield Button("", id="config-menu-api-key", classes="config-menu-item", compact=True)
            yield Button("", id="config-menu-native-model", classes="config-menu-item", compact=True)
            yield Button("", id="config-menu-groups", classes="config-menu-item", compact=True)
            yield Button("", id="config-menu-raw-enabled", classes="config-menu-item", compact=True)
            yield Button("", id="config-menu-raw-account", classes="config-menu-item", compact=True)
            yield Button("", id="config-menu-hourly-summary", classes="config-menu-item", compact=True)
            yield Button("", id="config-menu-daily-summary", classes="config-menu-item", compact=True)
            yield Button("", id="config-menu-member-kb", classes="config-menu-item", compact=True)
            yield Static(
                "隐私提示：成员知识库会把群聊原文和画像发送给已配置的模型 API；"
                "敏感推断可能用于群回复和总结。",
                id="config-member-kb-warning",
            )
            yield Button("", id="config-menu-proactive-mode", classes="config-menu-item", compact=True)
            yield Button("", id="config-menu-probability", classes="config-menu-item", compact=True)
            yield Button("", id="config-menu-mention-policy", classes="config-menu-item", compact=True)
            yield Button("", id="config-menu-continuation-enabled", classes="config-menu-item", compact=True)
            yield Button("", id="config-menu-continuation-max", classes="config-menu-item", compact=True)
            yield Button("", id="config-menu-continuation-delay", classes="config-menu-item", compact=True)
            yield Button("", id="config-menu-continuation-ttl", classes="config-menu-item", compact=True)
            yield Button(
                "保存到 .env，并重启调度进程",
                id="config-menu-save",
                classes="config-menu-item",
                variant="primary",
                compact=True,
            )
            yield Button("取消", id="config-menu-cancel", classes="config-menu-item", compact=True)
            yield Static(
                "鼠标点击条目进入编辑；上下键/Tab 切换，回车进入；点保存按钮或按 Ctrl+S 保存；Esc 取消",
                id="config-editor-help",
            )

    def on_mount(self) -> None:
        self._refresh_menu()
        self.query_one("#config-menu-api-endpoint", Button).focus()

    def on_key(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.key == "down":
            event.stop()
            self._focus_next()
        elif event.key == "up":
            event.stop()
            self._focus_previous()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        match event.button.id:
            case "config-menu-api-endpoint":
                self.app.push_screen(
                    ConfigValueScreen(title="OpenAI 兼容 API 地址", value=self._llm_endpoint),
                    self._on_llm_endpoint_changed,
                )
            case "config-menu-api-key":
                self.app.push_screen(
                    ConfigValueScreen(title="API Key（留空则保留当前值）", value="", password=True),
                    self._on_llm_api_key_changed,
                )
            case "config-menu-native-model":
                self.app.push_screen(
                    ConfigValueScreen(title="模型名称", value=self._llm_model),
                    self._on_llm_model_changed,
                )
            case "config-menu-groups":
                self.app.push_screen(
                    ConfigGroupSelectionScreen(self._config.available_groups, self._groups),
                    self._on_groups_changed,
                )
            case "config-menu-raw-enabled":
                self._raw_wechat_enabled = not self._raw_wechat_enabled
                self._refresh_menu()
                self._mark_dirty()
            case "config-menu-raw-account":
                self.app.push_screen(
                    ConfigValueScreen(title="微信账号匿名指纹", value=self._raw_wechat_account),
                    self._on_raw_account_changed,
                )
            case "config-menu-hourly-summary":
                self._hourly_summary_enabled = not self._hourly_summary_enabled
                self._refresh_menu()
                self._mark_dirty()
            case "config-menu-daily-summary":
                self._daily_summary_enabled = not self._daily_summary_enabled
                self._refresh_menu()
                self._mark_dirty()
            case "config-menu-member-kb":
                if self._member_kb_enabled:
                    self._member_kb_enabled = False
                    self._refresh_menu()
                    self._mark_dirty()
                else:
                    message_count, member_count, estimated_calls = self._member_kb_estimate()
                    self.app.push_screen(
                        MemberKnowledgeConsentScreen(
                            message_count=message_count,
                            member_count=member_count,
                            estimated_calls=estimated_calls,
                        ),
                        self._on_member_kb_consent,
                    )
            case "config-menu-proactive-mode":
                self.app.push_screen(
                    ConfigProactiveModeScreen(self._proactive_mode),
                    self._on_proactive_mode_changed,
                )
            case "config-menu-probability":
                self.app.push_screen(
                    ConfigValueScreen(
                        title="概率唤醒阈值",
                        value=_format_probability(self._agent_base_probability),
                    ),
                    self._on_probability_changed,
                )
            case "config-menu-mention-policy":
                self.app.push_screen(
                    ConfigMentionPolicyScreen(self._reply_mention_policy),
                    self._on_mention_policy_changed,
                )
            case "config-menu-continuation-enabled":
                self._continuation_enabled = not self._continuation_enabled
                self._refresh_menu()
                self._mark_dirty()
            case "config-menu-continuation-max":
                self.app.push_screen(
                    ConfigValueScreen(
                        title="Continuation max follow-ups",
                        value=str(self._continuation_max_followups),
                    ),
                    self._on_continuation_max_changed,
                )
            case "config-menu-continuation-delay":
                self.app.push_screen(
                    ConfigValueScreen(
                        title="Continuation delay seconds",
                        value=str(self._continuation_delay_seconds),
                    ),
                    self._on_continuation_delay_changed,
                )
            case "config-menu-continuation-ttl":
                self.app.push_screen(
                    ConfigValueScreen(
                        title="Continuation TTL seconds",
                        value=str(self._continuation_ttl_seconds),
                    ),
                    self._on_continuation_ttl_changed,
                )
            case "config-menu-save":
                self.action_save()
            case "config-menu-cancel":
                self.action_cancel()

    def action_save(self) -> None:
        llm_model = self._llm_model.strip()
        llm_endpoint = self._llm_endpoint.strip()
        openclaw_agent = self._openclaw_agent_id.strip()
        if not llm_model:
            self.query_one("#config-editor-help", Static).update("Native 模型不能为空")
            return
        if self._backend == "openclaw" and not openclaw_agent:
            self.query_one("#config-editor-help", Static).update("OpenClaw Agent ID 不能为空")
            return
        if not llm_endpoint:
            self.query_one("#config-editor-help", Static).update("API 地址不能为空")
            return
        if self._backend == "native" and not (
            self._config.native_configured or self._llm_api_key_update
        ):
            self.query_one("#config-editor-help", Static).update("Native 缺少 WO_LLM_API_KEY，不能切换")
            return
        if self._raw_wechat_enabled and not self._raw_wechat_account:
            self.query_one("#config-editor-help", Static).update("启用本地聊天库前请先选择账号指纹")
            return
        if (self._hourly_summary_enabled or self._daily_summary_enabled) and not self._groups:
            self.query_one("#config-editor-help", Static).update("启用自动总结前至少选择一个已授权群")
            return
        if self._continuation_ttl_seconds < self._continuation_delay_seconds:
            self.query_one("#config-editor-help", Static).update("Continuation TTL must be >= delay")
            return
        self.dismiss(
            AgentRuntimeConfig(
                backend=self._backend,
                proactive_mode=self._proactive_mode,
                llm_model=llm_model,
                openclaw_agent_id=openclaw_agent,
                agent_base_probability=self._agent_base_probability,
                reply_mention_policy=self._reply_mention_policy,
                continuation_enabled=self._continuation_enabled,
                continuation_max_followups=self._continuation_max_followups,
                continuation_delay_seconds=self._continuation_delay_seconds,
                continuation_ttl_seconds=self._continuation_ttl_seconds,
                native_configured=self._config.native_configured,
                openclaw_token_configured=self._config.openclaw_token_configured,
                openclaw_configured=self._config.openclaw_configured,
                pi_configured=self._config.pi_configured,
                llm_endpoint=llm_endpoint,
                llm_api_key_update=self._llm_api_key_update,
                groups=self._groups,
                available_groups=self._config.available_groups,
                raw_wechat_enabled=self._raw_wechat_enabled,
                raw_wechat_account=self._raw_wechat_account,
                hourly_summary_enabled=self._hourly_summary_enabled,
                daily_summary_enabled=self._daily_summary_enabled,
                member_kb_enabled=self._member_kb_enabled,
                member_kb_interval_seconds=self._config.member_kb_interval_seconds,
                member_kb_chunk_chars=self._config.member_kb_chunk_chars,
                member_kb_max_concurrency=self._config.member_kb_max_concurrency,
                member_kb_retries=self._config.member_kb_retries,
            )
        )

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _on_backend_changed(self, backend: str | None) -> None:
        if backend:
            self._backend = backend
            self._refresh_menu()
            self._mark_dirty()

    def _on_proactive_mode_changed(self, mode: str | None) -> None:
        if mode:
            self._proactive_mode = mode
            self._refresh_menu()
            self._mark_dirty()

    def _on_probability_changed(self, value: str | None) -> None:
        if value is None:
            return
        probability = _parse_probability(value)
        if probability is None:
            self.query_one("#config-editor-help", Static).update(
                "概率必须在 0 到 1 之间；可输入 0.05 或 5%。"
            )
            return
        self._agent_base_probability = probability
        self._refresh_menu()
        self._mark_dirty()

    def _on_mention_policy_changed(self, policy: str | None) -> None:
        if policy:
            self._reply_mention_policy = policy
            self._refresh_menu()
            self._mark_dirty()

    def _on_continuation_max_changed(self, value: str | None) -> None:
        parsed = _parse_int(value, minimum=0)
        if parsed is None:
            self.query_one("#config-editor-help", Static).update("Continuation max follow-ups must be >= 0")
            return
        self._continuation_max_followups = parsed
        self._refresh_menu()
        self._mark_dirty()

    def _on_continuation_delay_changed(self, value: str | None) -> None:
        parsed = _parse_int(value, minimum=5)
        if parsed is None:
            self.query_one("#config-editor-help", Static).update("Continuation delay must be >= 5 seconds")
            return
        self._continuation_delay_seconds = parsed
        if self._continuation_ttl_seconds < parsed:
            self._continuation_ttl_seconds = parsed
        self._refresh_menu()
        self._mark_dirty()

    def _on_continuation_ttl_changed(self, value: str | None) -> None:
        parsed = _parse_int(value, minimum=self._continuation_delay_seconds)
        if parsed is None:
            self.query_one("#config-editor-help", Static).update("Continuation TTL must be >= delay")
            return
        self._continuation_ttl_seconds = parsed
        self._refresh_menu()
        self._mark_dirty()

    def _on_llm_model_changed(self, value: str | None) -> None:
        if value is not None:
            self._llm_model = value
            self._refresh_menu()
            self._mark_dirty()

    def _on_llm_endpoint_changed(self, value: str | None) -> None:
        if value is not None:
            self._llm_endpoint = value
            self._refresh_menu()
            self._mark_dirty()

    def _on_llm_api_key_changed(self, value: str | None) -> None:
        if value:
            self._llm_api_key_update = value
            self._refresh_menu()
            self._mark_dirty()

    def _on_groups_changed(self, value: tuple[str, ...] | None) -> None:
        if value is not None:
            self._groups = value
            self._refresh_menu()
            self._mark_dirty()

    def _on_raw_account_changed(self, value: str | None) -> None:
        if value is not None:
            self._raw_wechat_account = value.strip().lower()
            self._refresh_menu()
            self._mark_dirty()

    def _on_member_kb_consent(self, accepted: bool) -> None:
        if not accepted:
            return
        self._member_kb_enabled = True
        self._refresh_menu()
        self._mark_dirty()

    def _member_kb_estimate(self) -> tuple[int, int, int]:
        if not self._groups:
            return (0, 0, 0)
        names = dict(self._config.available_groups)
        selectors = tuple(dict.fromkeys([*self._groups, *(names.get(item, item) for item in self._groups)]))
        placeholders = ",".join("?" for _ in selectors)
        try:
            with get_conn() as conn:
                row = conn.execute(
                    f"""
                    SELECT COUNT(*) AS message_count,
                           COUNT(DISTINCT group_id || char(31) ||
                               COALESCE(NULLIF(TRIM(sender_wxid), ''), '__unknown__')) AS member_count,
                           COALESCE(SUM(LENGTH(COALESCE(content_text, '')) +
                                        LENGTH(COALESCE(transcript, ''))), 0) AS content_chars
                      FROM messages
                     WHERE group_id IN ({placeholders}) OR group_name IN ({placeholders})
                    """,
                    [*selectors, *selectors],
                ).fetchone()
        except Exception:
            return (0, 0, 0)
        message_count = int(row["message_count"] or 0)
        member_count = int(row["member_count"] or 0)
        content_chars = int(row["content_chars"] or 0)
        chunks = (content_chars + self._config.member_kb_chunk_chars - 1) // self._config.member_kb_chunk_chars
        return (message_count, member_count, max(member_count, chunks) if message_count else 0)

    def _on_openclaw_agent_changed(self, value: str | None) -> None:
        if value is not None:
            self._openclaw_agent_id = value
            self._refresh_menu()
            self._mark_dirty()

    def _refresh_menu(self) -> None:
        self.query_one("#config-menu-api-endpoint", Button).label = (
            f"模型 API　{_clip(self._llm_endpoint, 52)}"
        )
        key_state = "本次将更新" if self._llm_api_key_update else (
            "已配置" if self._config.native_configured else "未配置"
        )
        self.query_one("#config-menu-api-key", Button).label = f"API Key　{key_state}"
        self.query_one("#config-menu-native-model", Button).label = (
            f"模型　{_clip(self._llm_model, 52)}"
        )
        self.query_one("#config-menu-groups", Button).label = (
            f"已选群　{len(self._groups)}/{len(self._config.available_groups)}"
        )
        self.query_one("#config-menu-raw-enabled", Button).label = (
            f"本地聊天库　{'on' if self._raw_wechat_enabled else 'off'}"
        )
        self.query_one("#config-menu-raw-account", Button).label = (
            f"微信账号　{self._raw_wechat_account or '未选择'}"
        )
        self.query_one("#config-menu-hourly-summary", Button).label = (
            f"每小时总结　{'on' if self._hourly_summary_enabled else 'off'}"
        )
        self.query_one("#config-menu-daily-summary", Button).label = (
            f"午夜每日总结　{'on' if self._daily_summary_enabled else 'off'}"
        )
        self.query_one("#config-menu-member-kb", Button).label = (
            f"成员知识库　{'on' if self._member_kb_enabled else 'off'}"
        )
        self.query_one("#config-menu-proactive-mode", Button).label = (
            f"主动模式　{_proactive_mode_label(self._proactive_mode)}"
        )
        self.query_one("#config-menu-probability", Button).label = (
            f"概率唤醒　{_probability_label(self._agent_base_probability)}"
        )
        self.query_one("#config-menu-mention-policy", Button).label = (
            f"@ 策略　{_mention_policy_label(self._reply_mention_policy)}"
        )
        self.query_one("#config-menu-continuation-enabled", Button).label = (
            f"Continuation：{'on' if self._continuation_enabled else 'off'}"
        )
        self.query_one("#config-menu-continuation-max", Button).label = (
            f"Follow-ups：{self._continuation_max_followups}"
        )
        self.query_one("#config-menu-continuation-delay", Button).label = (
            f"Follow-up delay：{self._continuation_delay_seconds}s"
        )
        self.query_one("#config-menu-continuation-ttl", Button).label = (
            f"Follow-up TTL：{self._continuation_ttl_seconds}s"
        )

    def _mark_dirty(self) -> None:
        self.query_one("#config-editor-save-hint", Static).update(
            "有未保存修改。请点击“保存到 .env，并重启调度进程”，或按 Ctrl+S。"
        )

    def _focus_controls(self) -> list[Button]:
        return [
            self.query_one("#config-menu-api-endpoint", Button),
            self.query_one("#config-menu-api-key", Button),
            self.query_one("#config-menu-native-model", Button),
            self.query_one("#config-menu-groups", Button),
            self.query_one("#config-menu-raw-enabled", Button),
            self.query_one("#config-menu-raw-account", Button),
            self.query_one("#config-menu-hourly-summary", Button),
            self.query_one("#config-menu-daily-summary", Button),
            self.query_one("#config-menu-member-kb", Button),
            self.query_one("#config-menu-proactive-mode", Button),
            self.query_one("#config-menu-probability", Button),
            self.query_one("#config-menu-mention-policy", Button),
            self.query_one("#config-menu-continuation-enabled", Button),
            self.query_one("#config-menu-continuation-max", Button),
            self.query_one("#config-menu-continuation-delay", Button),
            self.query_one("#config-menu-continuation-ttl", Button),
            self.query_one("#config-menu-save", Button),
            self.query_one("#config-menu-cancel", Button),
        ]

    def _focus_next(self) -> None:
        controls = self._focus_controls()
        current = self.focused
        if current in controls:
            controls[(controls.index(current) + 1) % len(controls)].focus()

    def _focus_previous(self) -> None:
        controls = self._focus_controls()
        current = self.focused
        if current in controls:
            controls[(controls.index(current) - 1) % len(controls)].focus()


class RunDashboard(App[None]):
    """Fixed status panel, scrolling logs, and modal Local Ask."""

    TITLE = "WeChat Oracle"
    SUB_TITLE = "ops console"

    CSS = """
    Screen {
        layout: vertical;
    }

    #status {
        height: 9;
        padding: 0 1;
        border: round #1f6feb;
        background: #0b1118;
        color: $text;
    }

    #logs {
        height: 1fr;
        border: round #1b3f5f;
        background: #070b10;
    }

    GroupPickerScreen, AskScreen, MemoryEditorScreen, ConfigScreen, ConfigBackendScreen, ConfigProactiveModeScreen, ConfigMentionPolicyScreen, ConfigValueScreen, ConfigGroupSelectionScreen {
        align: center middle;
    }

    #group-picker {
        width: 92;
        height: 24;
        padding: 1 2;
        border: round #1f6feb;
        background: #0b1118;
    }

    #group-picker-title {
        height: 1;
        text-style: bold;
        color: $primary;
    }

    #group-picker-list {
        height: 1fr;
        margin-top: 1;
        margin-bottom: 1;
    }

    #group-picker-help {
        height: 1;
        color: $text-muted;
    }

    #ask-dialog {
        width: 84;
        height: 10;
        padding: 1 2;
        border: round #1f6feb;
        background: #0b1118;
    }

    #ask-dialog-title {
        height: 1;
        text-style: bold;
        color: $primary;
    }

    #ask-dialog-meta {
        height: 1;
        color: $text-muted;
    }

    #ask-dialog-input {
        height: 3;
        margin-top: 1;
        margin-bottom: 1;
    }

    #ask-dialog-help {
        height: 1;
        color: $text-muted;
    }

    #memory-editor {
        width: 110;
        height: 34;
        padding: 1 2;
        border: round #5ccfe6;
        background: #0b1118;
    }

    #memory-editor-title {
        height: 1;
        text-style: bold;
        color: $secondary;
    }

    #memory-editor-meta {
        height: 1;
        color: $text-muted;
    }

    #memory-editor-text {
        height: 1fr;
        margin-top: 1;
        margin-bottom: 1;
    }

    #memory-editor-help {
        height: 1;
        color: $text-muted;
    }

    #config-editor {
        width: 92;
        height: 90%;
        overflow-y: auto;
        padding: 1 2;
        border: round #5ccfe6;
        background: #0b1118;
    }

    #config-editor-title {
        height: 1;
        text-style: bold;
        color: $secondary;
    }

    #config-editor-meta {
        height: 1;
        color: $text-muted;
    }

    #config-editor-save-hint {
        height: 1;
        color: $warning;
    }

    .config-menu-item {
        width: 1fr;
        height: 1;
        margin-top: 0;
    }

    #config-editor-help {
        height: 2;
        margin-top: 1;
        color: $text-muted;
    }

    #config-backend-picker {
        width: 92;
        height: 11;
        padding: 1 2;
        border: round #5ccfe6;
        background: #0b1118;
    }

    #config-backend-title {
        height: 1;
        text-style: bold;
        color: $secondary;
    }

    #config-backend-buttons {
        height: 1;
        margin-top: 0;
    }

    #config-backend-help {
        height: 1;
        color: $text-muted;
    }

    #config-proactive-picker {
        width: 92;
        height: 13;
        padding: 1 2;
        border: round #5ccfe6;
        background: #0b1118;
    }

    #config-proactive-title {
        height: 1;
        text-style: bold;
        color: $secondary;
    }

    #config-proactive-buttons {
        height: 1;
        margin-top: 0;
    }

    #config-proactive-help {
        height: 2;
        color: $text-muted;
    }

    #config-mention-picker {
        width: 92;
        height: 13;
        padding: 1 2;
        border: round #5ccfe6;
        background: #0b1118;
    }

    #config-mention-title {
        height: 1;
        text-style: bold;
        color: $secondary;
    }

    #config-mention-buttons {
        height: 1;
        margin-top: 0;
    }

    #config-mention-help {
        height: 2;
        color: $text-muted;
    }

    #config-value-editor {
        width: 84;
        height: 10;
        padding: 1 2;
        border: round #5ccfe6;
        background: #0b1118;
    }

    ConfigGroupSelectionScreen #config-value-editor {
        height: 90%;
        overflow-y: auto;
    }

    #config-value-title {
        height: 1;
        text-style: bold;
        color: $secondary;
    }

    #config-value-input {
        height: 3;
        margin-top: 1;
    }

    #config-value-buttons {
        height: 3;
        margin-top: 1;
    }

    #config-value-help {
        height: 2;
        color: $text-muted;
    }
    """

    BINDINGS = [
        ("a", "ask_selected_group", "询问"),
        ("g", "select_group", "选群"),
        ("m", "edit_memory", "记忆"),
        ("k", "member_knowledge", "成员库"),
        ("c", "edit_config", "配置"),
        ("w", "toggle_write_mode", "写入"),
        ("q", "quit", "退出"),
        ("ctrl+c", "quit", "退出"),
    ]

    def __init__(
        self,
        *,
        status_provider: Callable[[], list[str]],
        log_buffer: deque[str],
        agent_config_provider: Callable[[], AgentRuntimeConfig] | None = None,
        agent_config_save: Callable[[AgentRuntimeConfig], None] | None = None,
    ) -> None:
        super().__init__()
        self._status_provider = status_provider
        self._log_buffer = log_buffer
        self._agent_config_provider = agent_config_provider or load_agent_runtime_config
        self._agent_config_save = agent_config_save
        self._last_rendered_log_entry: str | None = None
        self._selected_group: LocalAskGroup | None = None
        self._allow_writes = False
        self._ask_running = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical():
            yield Static("", id="status")
            yield RichLog(id="logs", max_lines=1000, wrap=True, highlight=False, markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self._select_default_group()
        self.set_interval(1.0, self._refresh_status)
        self.set_interval(0.25, self._flush_logs)
        self._refresh_status()
        self._flush_logs()

    def action_ask_selected_group(self) -> None:
        if self._ask_running:
            self._append_local("本地问答正在运行")
            return
        if self._selected_group is None:
            self._select_default_group()
        if self._selected_group is None:
            self._append_local("请先按 g 选择群")
            return
        self.push_screen(
            AskScreen(group=self._selected_group, allow_writes=self._allow_writes),
            self._on_ask_submitted,
        )

    def _on_ask_submitted(self, question: str | None) -> None:
        if question:
            self._start_local_ask(question)

    def action_select_group(self) -> None:
        try:
            with get_conn() as conn:
                groups = list_local_ask_groups(conn, limit=50)
        except Exception as e:
            self._append_local(f"群列表读取失败：{type(e).__name__}: {e}")
            return
        if not groups:
            self._append_local("消息库里没有群聊")
            return
        self.push_screen(GroupPickerScreen(groups), self._on_group_picked)

    def _on_group_picked(self, group: LocalAskGroup | None) -> None:
        if group is None:
            return
        self._selected_group = group
        self._append_local(f"已选择群：{group.label}")

    def action_toggle_write_mode(self) -> None:
        self._allow_writes = not self._allow_writes
        self._append_local(f"本地问答写记忆模式：{'开启' if self._allow_writes else '关闭'}")

    def action_edit_memory(self) -> None:
        if self._selected_group is None:
            self._select_default_group()
        if self._selected_group is None:
            self._append_local("请先按 g 选择群")
            return
        try:
            with get_conn() as conn:
                group_memory = get_group_memory(conn, self._selected_group.group_id)
                persona_drift = get_persona_drift(conn, self._selected_group.group_id)
        except Exception as e:
            self._append_local(f"记忆读取失败：{type(e).__name__}: {e}")
            return
        self.push_screen(
            MemoryEditorScreen(
                group=self._selected_group,
                group_memory=group_memory,
                persona_drift=persona_drift,
            ),
            self._on_memory_editor_saved,
        )

    def action_member_knowledge(self) -> None:
        if self._selected_group is None:
            self._select_default_group()
        if self._selected_group is None:
            self._append_local("请先按 g 选择群")
            return
        self.push_screen(MemberKnowledgeScreen(self._selected_group))

    def action_edit_config(self) -> None:
        try:
            config = self._agent_config_provider()
        except Exception as e:
            self._append_local(f"配置读取失败：{type(e).__name__}: {e}")
            return
        self.push_screen(ConfigScreen(config), self._on_config_saved)

    def _on_config_saved(self, result: AgentRuntimeConfig | None) -> None:
        if result is None:
            return
        if self._agent_config_save is None:
            self._append_local("配置保存失败：当前运行方式没有提供保存回调")
            return
        try:
            self._agent_config_save(result)
        except Exception as e:
            self._append_local(f"配置保存失败：{type(e).__name__}: {e}")
            return
        self._append_local(
            f"配置已保存：后端 {result.backend}，Native 模型 {result.llm_model}，"
            f"OpenClaw Agent {result.openclaw_agent_id}"
        )

    def _on_memory_editor_saved(self, result: MemoryEditResult | None) -> None:
        if result is None:
            return
        if self._selected_group is None:
            self._append_local("记忆保存失败：没有选中的群")
            return
        group_id = self._selected_group.group_id
        if len(result.group_memory) > settings.agent_memory_max_chars:
            self._append_local(
                f"记忆保存失败：group_memory {len(result.group_memory)} 字，"
                f"上限 {settings.agent_memory_max_chars} 字"
            )
            return
        if len(result.persona_drift) > 4000:
            self._append_local(
                f"记忆保存失败：persona_drift {len(result.persona_drift)} 字，上限 4000 字"
            )
            return
        try:
            with get_conn() as conn:
                before_memory = get_group_memory(conn, group_id)
                before_drift = get_persona_drift(conn, group_id)
                with transaction(conn):
                    upsert_group_memory(conn, group_id, result.group_memory)
                    upsert_persona_drift(conn, group_id, result.persona_drift)
        except Exception as e:
            self._append_local(f"记忆保存失败：{type(e).__name__}: {e}")
            return
        append_event(
            "memory.manual_edit",
            group_id=group_id,
            group_name=self._selected_group.group_name,
            group_memory_prev_len=len(before_memory),
            group_memory_new_len=len(result.group_memory),
            persona_drift_prev_len=len(before_drift),
            persona_drift_new_len=len(result.persona_drift),
        )
        self._append_local(
            "记忆已保存："
            f"group_memory {len(before_memory)}->{len(result.group_memory)} 字，"
            f"persona_drift {len(before_drift)}->{len(result.persona_drift)} 字"
        )

    def _refresh_status(self) -> None:
        try:
            status = self.query_one("#status", Static)
        except NoMatches:
            return
        lines = list(self._status_provider())
        lines.extend(self._local_ask_status_lines())
        status.update("\n".join(lines))

    def _flush_logs(self) -> None:
        try:
            log = self.query_one("#logs", RichLog)
        except NoMatches:
            return
        lines = list(self._log_buffer)
        if not lines:
            log.clear()
            self._last_rendered_log_entry = None
            return
        start = 0
        if self._last_rendered_log_entry is not None:
            # Match the exact deque entry, not its text; duplicate log lines are common.
            for i in range(len(lines) - 1, -1, -1):
                if lines[i] is self._last_rendered_log_entry:
                    start = i + 1
                    break
            else:
                log.clear()
        for line in lines[start:]:
            log.write(line)
        self._last_rendered_log_entry = lines[-1]

    def _append_local(self, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self._log_buffer.append(f"{stamp} {'LOCAL':<8s} │ {text}")

    def _local_ask_status_lines(self) -> list[str]:
        if self._selected_group is None:
            group = f"[{_MUTED_STYLE}]未选择[/]"
            detail = "hint press g to select"
        else:
            group = _markup_clip(self._selected_group.short_label, 28)
            detail = (
                f"messages {self._selected_group.msg_count} {_SEP} "
                f"last {escape(self._selected_group.last_seen)} {_SEP} "
                f"ID：{_markup_clip(self._selected_group.group_id, 28)}"
            )
        mode = _tag("write", _WARN_STYLE) if self._allow_writes else _tag("read-only", _OK_STYLE)
        state = _tag("running", _WARN_STYLE) if self._ask_running else _tag("standby", _OK_STYLE)
        return [
            _status_row(
                "LOCAL",
                f"group {_status_value(group)} {_SEP} mode {mode} {_SEP} state {state} {_SEP} {detail}",
            ),
        ]

    def _select_default_group(self) -> None:
        try:
            with get_conn() as conn:
                self._selected_group = resolve_local_ask_group(conn, None)
        except Exception as e:
            self._append_local(f"默认群选择失败：{type(e).__name__}: {e}")
            return
        self._append_local(f"已选择群：{self._selected_group.label}")

    def _start_local_ask(self, question: str) -> None:
        question = question.strip()
        if not question:
            return
        if self._ask_running:
            self._append_local("本地问答正在运行")
            return
        if self._selected_group is None:
            self._select_default_group()
        if self._selected_group is None:
            self._append_local("请先按 g 选择群")
            return
        group_id = self._selected_group.group_id
        group_label = self._selected_group.short_label
        allow_writes = self._allow_writes
        self._ask_running = True
        self._append_local(
            f"询问「{group_label}」；写记忆：{'开' if allow_writes else '关'}；问题：{question}"
        )

        def run() -> None:
            try:
                result = run_local_ask(
                    group_selector=group_id,
                    question=question,
                    allow_writes=allow_writes,
                    log_path=settings.data_dir / "dispatcher.log",
                    llm_log_path=settings.data_dir / "llm_debug.log",
                )
            except Exception as e:
                self._append_local(f"询问失败：{type(e).__name__}: {e}")
                request_balance_refresh()
            else:
                reply = result.reply_text or "（无回复）"
                for idx, line in enumerate(reply.splitlines() or [reply]):
                    prefix = "回复： " if idx == 0 else "      "
                    self._append_local(prefix + line)
                self._append_local(f"完成，用时 {result.duration_s:.1f}s")
                request_balance_refresh()
            finally:
                self._ask_running = False

        threading.Thread(target=run, name="wechat-oracle-local-ask", daemon=True).start()


def status_lines_for_processes(
    process_rows: list[tuple[str, int | None, int | None]],
    agent_config: AgentRuntimeConfig | None = None,
) -> list[str]:
    """Build status text for the dashboard.

    `process_rows` entries are `(name, pid, exit_code)`; `exit_code is None`
    means the process is still running.
    """
    try:
        with get_conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            groups = conn.execute(
                "SELECT COUNT(DISTINCT group_id) FROM messages WHERE group_id IS NOT NULL"
            ).fetchone()[0]
    except Exception as e:
        total = "?"
        groups = f"? ({type(e).__name__})"

    agent_config = agent_config or load_agent_runtime_config()
    backend = (agent_config.backend or "native").lower()
    agent_label = (
        f"openclaw/{agent_config.openclaw_agent_id}"
        if backend == "openclaw"
        else (f"pi/{settings.pi_model}" if backend == "pi" else f"native/{agent_config.llm_model}")
    )
    balance_label = _balance_label(backend, native_configured=agent_config.native_configured)
    proc_bits = []
    for name, pid, exit_code in process_rows:
        state = (
            _tag(f"running PID {pid}", _OK_STYLE)
            if exit_code is None
            else _tag(f"exited {exit_code}", _BAD_STYLE)
        )
        proc_bits.append(f"{escape(_process_label(name))} {state}")
    watch_label = "all groups" if not settings.groups else ", ".join(settings.groups)
    bot_label = settings.bot_name or "unset"
    reply_label = _tag(settings.reply_backend, _OK_STYLE) if settings.reply else _tag("off", _MUTED_STYLE)
    lurk_label = _tag("on", _OK_STYLE) if settings.agent_lurk_enabled else _tag("off", _MUTED_STYLE)
    member_kb_label = _tag("on", _WARN_STYLE) if agent_config.member_kb_enabled else _tag("off", _MUTED_STYLE)
    stance_label = _proactive_status_label(agent_config.proactive_mode)
    wake_label = _wake_status_label(
        agent_config.agent_base_probability, agent_config.proactive_mode
    )
    mention_label = _mention_status_label(agent_config.reply_mention_policy)
    continuation_label = _continuation_status_label(agent_config)
    return [
        f"[bold #5ccfe6]WeChat Oracle[/] [#245b73]//[/] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        _status_row(
            "BOT",
            f"name {_markup_clip(bot_label, 24)} {_SEP} "
            f"agent {_markup_clip(agent_label, 40)} {_SEP} "
            f"balance {balance_label} {_SEP} reply {reply_label} {_SEP} @ {mention_label}",
        ),
        _status_row(
            "AMBIENT",
            f"stance {stance_label} {_SEP} wake {wake_label} {_SEP} cont {continuation_label} {_SEP} lurk {lurk_label} {_SEP} member-kb {member_kb_label}",
        ),
        _status_row(
            "WATCH",
            f"groups {_markup_clip(watch_label, 68)} {_SEP} WeFlow {_markup_clip(settings.weflow_base_url, 32)}",
        ),
        _status_row(
            "DB",
            f"messages {total} {_SEP} groups {groups} {_SEP} path {_markup_clip(str(settings.db_path), 52)}",
        ),
        _status_row("PROC", f" {_SEP} ".join(proc_bits) if proc_bits else "not started"),
    ]


def _balance_label(backend: str, *, native_configured: bool | None = None) -> str:
    if backend != "native":
        return f"[{_MUTED_STYLE}]-[/]"
    if native_configured is False or (native_configured is None and not settings.llm_api_key):
        return _tag("no key", _BAD_STYLE)
    now = time.time()
    with _BALANCE_LOCK:
        stale = now - _BALANCE_STATUS.updated_at > _BALANCE_REFRESH_SECONDS
        should_refresh = _mark_balance_refresh_locked() if stale else False
        label = _BALANCE_STATUS.label
        refreshing = _BALANCE_STATUS.refreshing
    if should_refresh:
        _start_balance_refresh_thread()
    if label.startswith("error:"):
        shown = _tag(label, _BAD_STYLE)
    elif label == "loading":
        shown = _tag("loading", _WARN_STYLE)
    else:
        shown = _tag(label, _OK_STYLE)
    return shown + ("*" if refreshing and label != "loading" else "")


def request_balance_refresh() -> None:
    """Force an async native balance refresh after a known model call."""
    if (settings.agent_backend or "native").lower() != "native":
        return
    if not settings.llm_api_key:
        return
    with _BALANCE_LOCK:
        should_refresh = _mark_balance_refresh_locked(force=True)
    if should_refresh:
        _start_balance_refresh_thread()


def _mark_balance_refresh_locked(*, force: bool = False) -> bool:
    if _BALANCE_STATUS.refreshing:
        return False
    if not force and time.time() - _BALANCE_STATUS.updated_at <= _BALANCE_REFRESH_SECONDS:
        return False
    _BALANCE_STATUS.refreshing = True
    return True


def _start_balance_refresh_thread() -> None:
    threading.Thread(
        target=_refresh_balance,
        name="wechat-oracle-balance",
        daemon=True,
    ).start()


def _refresh_balance() -> None:
    try:
        from .dispatcher import fetch_llm_balance

        payload = fetch_llm_balance()
        label = _format_balance_label(payload)
    except Exception as e:
        label = f"error:{type(e).__name__}"
    with _BALANCE_LOCK:
        _BALANCE_STATUS.label = _clip(label, 24)
        _BALANCE_STATUS.updated_at = time.time()
        _BALANCE_STATUS.refreshing = False


def _format_balance_label(payload: dict[str, object]) -> str:
    available = payload.get("is_available")
    infos = payload.get("balance_infos")
    if not isinstance(infos, list) or not infos:
        return "available" if available else "unavailable"
    parts: list[str] = []
    for item in infos:
        if not isinstance(item, dict):
            continue
        currency = str(item.get("currency") or item.get("currency_code") or "").strip()
        total = item.get("total_balance")
        if total is None:
            continue
        prefix = f"{currency} " if currency else ""
        parts.append(prefix + str(total))
    if parts:
        return ", ".join(parts)
    return "available" if available else "unavailable"


def _clip(value: object, max_len: int) -> str:
    text = str(value)
    if len(text) <= max_len:
        return text
    return text[: max(0, max_len - 1)] + "~"


def _normalize_probability(value: object) -> float:
    try:
        probability = float(value)
    except (TypeError, ValueError):
        return 0.25
    if not 0.0 <= probability <= 1.0:
        return 0.25
    return probability


def _parse_probability(value: str) -> float | None:
    text = value.strip()
    if not text:
        return None
    try:
        if text.endswith("%"):
            probability = float(text[:-1].strip()) / 100.0
        else:
            probability = float(text)
    except ValueError:
        return None
    if not 0.0 <= probability <= 1.0:
        return None
    return probability


def _parse_int(value: str | None, *, minimum: int) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value.strip())
    except ValueError:
        return None
    if parsed < minimum:
        return None
    return parsed


def _format_probability(value: object) -> str:
    return f"{_normalize_probability(value):g}"


def _probability_label(value: object) -> str:
    probability = _normalize_probability(value)
    return f"{probability:g} ({probability * 100:.1f}%)"


def _percent_label(value: object) -> str:
    probability = _normalize_probability(value)
    if probability == 0:
        return "0%"
    if probability < 0.01:
        return f"{probability * 100:.2f}%"
    return f"{probability * 100:.1f}%"


def _markup_clip(value: object, max_len: int) -> str:
    return escape(_clip(value, max_len))


def _tag(text: object, style: str) -> str:
    return f"[{style}]{escape(str(text))}[/]"


def _status_row(label: str, body: str) -> str:
    return f"[{_LABEL_STYLE}]{escape(label):<7s}[/] {body}"


def _status_value(value: str) -> str:
    return f"[#d6deeb]{value}[/]"


def _process_label(name: str) -> str:
    return {
        "live": "INGEST",
        "dispatcher": "DISPATCH",
        "mm": "MEDIA",
        "run": "SYSTEM",
    }.get(name, name.upper())


def _backend_label(backend: str) -> str:
    if backend == "openclaw":
        return "OpenClaw：外部 Agent runtime"
    return "Native：本进程工具链"


def _proactive_mode_label(mode: str) -> str:
    match mode:
        case "off":
            return "Off：只响应 @ / 引用"
        case "proactive":
            return "Proactive：允许低频主动抛话题"
        case _:
            return "Reactive：只接当前话题"


def _proactive_status_label(mode: str) -> str:
    match mode:
        case "off":
            return _tag("off", _MUTED_STYLE)
        case "proactive":
            return _tag("proactive", _WARN_STYLE)
        case _:
            return _tag("reactive", _OK_STYLE)


def _mention_policy_label(policy: str) -> str:
    match policy:
        case "always":
            return "Always：所有群回复都 @"
        case "never":
            return "Never：群回复都不 @"
        case _:
            return "Explicit：只 @ 显式触发"


def _mention_status_label(policy: str) -> str:
    match policy:
        case "always":
            return _tag("always", _WARN_STYLE)
        case "never":
            return _tag("never", _MUTED_STYLE)
        case _:
            return _tag("explicit", _OK_STYLE)


def _continuation_status_label(config: AgentRuntimeConfig) -> str:
    if not config.continuation_enabled:
        return _tag("off", _MUTED_STYLE)
    return _tag(
        f"on {config.continuation_max_followups}x/{config.continuation_delay_seconds}s",
        _OK_STYLE,
    )


def _wake_status_label(probability: object, proactive_mode: str) -> str:
    value = _normalize_probability(probability)
    if proactive_mode == "off" or value <= 0:
        return _tag("off", _MUTED_STYLE)
    style = _WARN_STYLE if value >= 0.15 else _OK_STYLE
    return _tag(_percent_label(value), style)


def _current_marker(current: str, value: str) -> str:
    return "当前　" if current == value else "　　　"


def _format_group_item(group: LocalAskGroup) -> str:
    return (
        f"{set_cell_size(_clip(group.short_label, 28), 30)}"
        f"消息 {group.msg_count:>6d}  "
        f"最近 {set_cell_size(group.last_seen, 16)}  "
        f"ID {_clip(group.group_id, 28)}"
    )

import ast
import inspect

import pytest

from wechat_oracle.config import settings
from wechat_oracle.replier import (
    ReplySendError,
    UiaDirectReplier,
    Wx4pyReplier,
    build_replier,
)


class FakeChatWindow:
    def send_to(self, *args, **kwargs):
        raise AssertionError("send_to must not be reached")


class FakeWx:
    chat_window = FakeChatWindow()


def test_disallowed_group_is_rejected_before_ui_input(monkeypatch) -> None:
    monkeypatch.setattr(settings, "reply_allowed_groups", ["人心黄黄"])
    with pytest.raises(ReplySendError, match="not in WO_REPLY_ALLOWED_GROUPS"):
        Wx4pyReplier(FakeWx()).send("别的群", None, "测试")


def test_failed_real_mention_never_falls_back_to_plain_text(monkeypatch) -> None:
    monkeypatch.setattr(settings, "reply_allowed_groups", ["人心黄黄"])
    monkeypatch.setattr("wechat_oracle.replier._windows_input_desktop_available", lambda: True)
    replier = Wx4pyReplier(FakeWx())
    monkeypatch.setattr(replier, "_send_group_mention", lambda *args: False)
    with pytest.raises(ReplySendError, match="refusing plain-text fallback"):
        replier.send("人心黄黄", "群友", "测试")


class FakeValuePattern:
    IsReadOnly = False

    def __init__(self) -> None:
        self.Value = ""

    def SetValue(self, value: str) -> bool:
        self.Value = value
        return True


class FakeEdit:
    def __init__(self, *, focus_ok: bool = True) -> None:
        self._focus_ok = focus_ok
        self._focused = False
        self.value_pattern = FakeValuePattern()
        self.keys: list[str] = []

    @property
    def HasKeyboardFocus(self) -> bool:
        return self._focused

    def SetFocus(self) -> bool:
        self._focused = self._focus_ok
        return self._focus_ok

    def GetValuePattern(self):
        return self.value_pattern

    def SendKeys(self, keys: str) -> None:
        self.keys.append(keys)

    def Click(self, *args, **kwargs) -> None:
        raise AssertionError("uia-direct must never click the chat input")


class FakeSelectionPattern:
    def __init__(
        self, control, *, select_ok: bool = True, changes_selection: bool = True
    ) -> None:
        self.control = control
        self.select_ok = select_ok
        self.changes_selection = changes_selection
        self.calls = 0

    @property
    def IsSelected(self) -> bool:
        return self.control.selected

    def Select(self) -> bool:
        self.calls += 1
        if self.select_ok and self.changes_selection:
            self.control.selected = True
        return self.select_ok


class FakeLegacyPattern:
    def __init__(self, control, *, action_ok: bool = False) -> None:
        self.control = control
        self.action_ok = action_ok
        self.calls = 0

    def DoDefaultAction(self, *, waitTime: float = 0) -> bool:
        self.calls += 1
        if self.action_ok:
            self.control.selected = True
        return self.action_ok


class FakeSessionControl:
    def __init__(
        self,
        automation_id: str,
        *,
        legacy_ok: bool = False,
        select_ok: bool = True,
        selection_changes: bool = True,
    ) -> None:
        self.AutomationId = automation_id
        self.selected = False
        self.legacy = FakeLegacyPattern(self, action_ok=legacy_ok)
        self.selection = FakeSelectionPattern(
            self,
            select_ok=select_ok,
            changes_selection=selection_changes,
        )

    @property
    def HasKeyboardFocus(self) -> bool:
        return False

    def Exists(self, **kwargs) -> bool:
        return True

    def GetSelectionItemPattern(self):
        return self.selection

    def GetLegacyIAccessiblePattern(self):
        return self.legacy

    def GetInvokePattern(self):
        return None

    def SetFocus(self) -> bool:
        return False

    def SendKeys(self, keys: str) -> None:
        raise AssertionError("unfocused session must not receive keys")

    def Click(self, *args, **kwargs) -> None:
        raise AssertionError("uia-direct must never click a session")

    def DoubleClick(self, *args, **kwargs) -> None:
        raise AssertionError("uia-direct must never double-click a session")


class FakeRoot:
    def __init__(self, session: FakeSessionControl) -> None:
        self.session = session

    def ListItemControl(self, **kwargs):
        return self.session


class FakeWindow:
    def __init__(self) -> None:
        self.activations = 0

    def activate(self) -> None:
        self.activations += 1


class FakeDirectChat:
    def __init__(self, session: FakeSessionControl, edit: FakeEdit) -> None:
        self.root = FakeRoot(session)
        self._window = FakeWindow()
        self.edit = edit

    def _get_chat_input(self):
        return self.edit

    def paste_text_into_focused_input(self, *args, **kwargs) -> bool:
        raise AssertionError("ValuePattern should avoid clipboard fallback")

    def send_to(self, *args, **kwargs) -> bool:
        raise AssertionError("uia-direct must never call wx4py send_to")


class FakeDirectWx:
    def __init__(self, chat: FakeDirectChat) -> None:
        self.chat_window = chat


def test_uia_direct_sends_via_accessibility_and_value_patterns_without_click(
    monkeypatch,
) -> None:
    group = "group-a"
    session = FakeSessionControl(f"session_item_{group}", legacy_ok=True)
    edit = FakeEdit()
    chat = FakeDirectChat(session, edit)
    monkeypatch.setattr(settings, "reply_allowed_groups", [group])
    monkeypatch.setattr("wechat_oracle.replier._windows_input_desktop_available", lambda: True)

    UiaDirectReplier(FakeDirectWx(chat)).send(group, None, "hello")

    assert session.selected is True
    assert session.legacy.calls == 1
    assert session.selection.calls == 0
    assert edit.value_pattern.Value == "hello"
    assert edit.keys == ["{Enter}"]
    assert chat._window.activations == 1


def test_uia_direct_refuses_when_exact_session_cannot_be_selected(monkeypatch) -> None:
    group = "group-a"
    session = FakeSessionControl(
        f"session_item_{group}",
        select_ok=True,
        selection_changes=False,
    )
    edit = FakeEdit()
    monkeypatch.setattr(settings, "reply_allowed_groups", [group])
    monkeypatch.setattr("wechat_oracle.replier._windows_input_desktop_available", lambda: True)

    with pytest.raises(ReplySendError, match="could not select exact group"):
        UiaDirectReplier(FakeDirectWx(FakeDirectChat(session, edit))).send(
            group, None, "hello"
        )

    assert edit.value_pattern.Value == ""
    assert edit.keys == []


def test_uia_direct_refuses_submit_when_input_cannot_take_focus(monkeypatch) -> None:
    group = "group-a"
    session = FakeSessionControl(f"session_item_{group}")
    edit = FakeEdit(focus_ok=False)
    monkeypatch.setattr(settings, "reply_allowed_groups", [group])
    monkeypatch.setattr("wechat_oracle.replier._windows_input_desktop_available", lambda: True)

    with pytest.raises(ReplySendError, match="focus and clear"):
        UiaDirectReplier(FakeDirectWx(FakeDirectChat(session, edit))).send(
            group, None, "hello"
        )

    assert edit.keys == []


def test_build_replier_selects_uia_direct_backend(monkeypatch) -> None:
    sentinel = object()
    monkeypatch.setattr(settings, "reply", True)
    monkeypatch.setattr(settings, "reply_backend", "uia-direct")
    monkeypatch.setattr(settings, "reply_allowed_groups", ["group-a"])
    monkeypatch.setattr(
        UiaDirectReplier,
        "try_connect",
        classmethod(lambda cls: sentinel),
    )

    assert build_replier() is sentinel


def test_uia_direct_class_has_no_mouse_public_send_or_mutating_connect_calls() -> None:
    tree = ast.parse(inspect.getsource(UiaDirectReplier))
    called_attributes = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert called_attributes.isdisjoint({"Click", "DoubleClick", "send_to", "connect"})

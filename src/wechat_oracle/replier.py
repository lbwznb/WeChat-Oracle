"""Reply backends for the dispatcher.

The dispatcher generates a text reply, then needs to put it back into the
WeChat conversation. Three backends:

  - `Wx4pyReplier`   — default. Drives WeChat's UI via wx4py (Windows only;
    requires main window visible). Identifies target groups by display name.
  - `UiaDirectReplier` — no-mouse wx4py path. Selects exact UI Automation
    controls and refuses to fall back to Click/DoubleClick/send_to.
  - `StdoutReplier`  — no-op fallback. Prints to logs only. Used when
    WO_REPLY=False or wx4py fails to initialize.

(A Tencent iLink Bot HTTP backend was prototyped but proven incapable of
group delivery — see README "实验记录" section.)

`build_replier()` is the single factory call from the dispatcher. It reads
`settings.reply_backend` and tries the chosen backend; on failure (wx4py
can't connect, etc.) it warns and degrades to StdoutReplier so the
dispatcher loop still runs.

Adding a backend: implement the `Replier` Protocol (just `send` and
`disconnect`) and add a branch in `build_replier()`. No dispatcher change
needed — that's the whole point of this file.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Protocol

from loguru import logger

from .config import settings


def _configure_wx4py_loggers() -> None:
    level = getattr(logging, settings.wx4py_log_level.upper(), logging.WARNING)
    for name in (
        "wx4py",
        "wx4py.client",
        "wx4py.core",
        "wx4py.core.window",
        "wx4py.features",
        "wx4py.features.chat",
    ):
        logging.getLogger(name).setLevel(level)


def _with_wx4py_info_suppressed(fn):
    if settings.wx4py_log_level.upper() in ("DEBUG", "INFO"):
        return fn()
    old_disable = logging.root.manager.disable
    logging.disable(logging.INFO)
    try:
        return fn()
    finally:
        logging.disable(old_disable)


# Four-per-em space — WeChat displays this after a selected @ mention.
_AT_SEP = " "


def _strip_leading_requester_mention(text: str, requester: str | None) -> str:
    """Avoid double-@ when the model starts its reply with @requester.

    `Wx4pyReplier.send` already prefixes outgoing group replies with a real
    WeChat mention. The LLM still occasionally imitates prior bot messages
    and emits "@张三 ..." itself; strip only that exact leading requester
    mention and leave all other text untouched.
    """
    if not requester:
        return text
    body = text.lstrip()
    prefix = f"@{requester}"
    if not body.startswith(prefix):
        return text
    body = body[len(prefix):]
    body = body.lstrip(" \t\r\n\u2005")
    return body


class Replier(Protocol):
    """The dispatcher only needs these two ops."""

    def send(self, group_name: str | None, requester: str | None, text: str) -> None: ...
    def disconnect(self) -> None: ...


class ReplySendError(RuntimeError):
    """Raised when a replier can prove the outgoing message was not delivered."""


# ---- stdout (always-available fallback) -----------------------------------


class StdoutReplier:
    """Drop messages on the floor (after they've been logged elsewhere).
    Used when WO_REPLY=False, or as a graceful degradation when the chosen
    backend can't connect."""

    def send(self, group_name: str | None, requester: str | None, text: str) -> None:
        logger.debug("stdout-replier: would send to {}: {!r}", group_name, text[:80])

    def disconnect(self) -> None:
        pass


# ---- wx4py (current default) ----------------------------------------------


class Wx4pyReplier:
    """UI-automation backend. `_wx` is the connected wx4py.WeChatClient.

    We do NOT verify per-group nickname at startup. The previous check
    (wx4py.group_manager.get_group_nickname per group) cost 5–30s each via
    UI tab-walk and was only a soft warning. If you logged into the wrong
    WeChat account, you'll notice from the WeChat sidebar in seconds —
    cheaper than the startup tax.
    """

    def __init__(self, wx) -> None:
        self._wx = wx

    def send(self, group_name: str | None, requester: str | None, text: str) -> None:
        if not group_name:
            raise ReplySendError("missing group name")
        if group_name not in settings.reply_allowed_groups:
            raise ReplySendError(
                f"group {group_name!r} is not in WO_REPLY_ALLOWED_GROUPS; refusing UI send"
            )
        if os.name == "nt" and not _windows_input_desktop_available():
            raise ReplySendError("Windows input desktop is locked or unavailable; refusing UI send")
        text = _strip_leading_requester_mention(text, requester)
        if requester:
            if self._send_group_mention(group_name, requester, text):
                return
            raise ReplySendError(
                f"could not verify a real mention for {requester!r}; refusing plain-text fallback"
            )

        body = f"@{requester}{_AT_SEP}{text}" if requester else text
        self._send_plain_group(group_name, body)

    def _send_plain_group(self, group_name: str, body: str) -> None:
        """Send one plain group message through wx4py's ordinary UI path."""
        try:
            sent = self._wx.chat_window.send_to(group_name, body, target_type="group")
        except Exception as e:
            raise ReplySendError(f"wx4py send_to failed for {group_name!r}: {e}") from e
        if not sent:
            raise ReplySendError(f"wx4py send_to returned false for {group_name!r}")

    def _send_group_mention(self, group_name: str, requester: str, text: str) -> bool:
        """Send a real WeChat group @ mention.

        wx4py's public `send_to()` pastes the whole message in one shot. In
        WeChat that produces literal "@name" text, not a notification. To
        create the rich mention token, the input box must receive a typed "@",
        the member name, and a selection key before the rest of the message is
        pasted.
        """
        try:
            chat = self._wx.chat_window
            if not chat.open_chat(group_name, target_type="group"):
                return False
            edit = chat._get_chat_input()
            edit = chat.prepare_input_for_paste(edit)
            if not edit:
                return False

            before_candidates = self._mention_candidate_signatures(chat.root, requester)
            edit.SendKeys("@")
            time.sleep(0.2)
            if not chat.paste_text_into_focused_input(
                requester, log_error="写入 @ 对象到剪贴板失败"
            ):
                self._clear_input(edit)
                return False
            time.sleep(0.5)
            after_candidates = self._mention_candidate_signatures(chat.root, requester)
            if not (after_candidates - before_candidates):
                logger.warning(
                    "wx4py @ candidate not visible for requester={!r}", requester
                )
                self._clear_input(edit)
                return False
            edit.SendKeys("{Enter}")
            time.sleep(0.2)

            suffix = text.strip()
            if suffix:
                if not chat.paste_text_into_focused_input(
                    _AT_SEP + suffix,
                    log_error="写入 @ 回复正文到剪贴板失败",
                ):
                    self._clear_input(edit)
                    return False
            edit.SendKeys("{Enter}")
            time.sleep(0.3)
            return True
        except Exception as e:
            try:
                self._clear_input(edit)
            except (NameError, UnboundLocalError):
                pass
            logger.warning(
                "wx4py real @ mention failed (group={!r}, requester={!r}); "
                "send will be refused: {}",
                group_name, requester, e,
            )
            return False

    @staticmethod
    def _clear_input(edit) -> None:
        """Best-effort cleanup after an aborted mention composition."""
        try:
            edit.SetFocus()
            edit.SendKeys("{Ctrl}a")
            edit.SendKeys("{Delete}")
        except Exception as exc:
            logger.warning("failed to clear aborted WeChat input: {}", exc)

    def _mention_candidate_signatures(self, root, requester: str) -> set[tuple[str, str, int, int]]:
        """Best-effort snapshot of visible controls that look like @ candidates.

        Without this guard, pressing Enter after a failed @ lookup can send the
        bare "@name" text. We take a before/after snapshot and require a new
        matching non-edit control to appear, so existing chat messages that
        happen to contain the requester's name do not count.
        """
        target = requester.strip()
        if not target:
            return set()
        found: set[tuple[str, str, int, int]] = set()

        def walk(ctrl, depth: int) -> None:
            if depth > 8 or ctrl is None:
                return
            try:
                name = (ctrl.Name or "").strip()
                control_type = ctrl.ControlTypeName or ""
                if target in name and control_type != "EditControl":
                    rect = ctrl.BoundingRectangle
                    top = int(getattr(rect, "top", 0) or 0)
                    left = int(getattr(rect, "left", 0) or 0)
                    found.add((name, control_type, left, top))
            except Exception:
                pass
            try:
                children = ctrl.GetChildren()
            except Exception:
                return
            for child in children:
                walk(child, depth + 1)

        walk(root, 0)
        return found

    def disconnect(self) -> None:
        try:
            self._wx.disconnect()
        except Exception as e:
            logger.warning("wx4py disconnect failed: {}", e)

    @classmethod
    def try_connect(cls) -> Replier | None:
        """Returns a connected Wx4pyReplier or None if wx4py is unhappy.
        Caller decides whether to fall back to stdout."""
        _configure_wx4py_loggers()
        try:
            from wx4py import WeChatClient
        except ImportError:
            logger.warning("wx4py not installed; can't use wx4py backend")
            return None
        _configure_wx4py_loggers()
        try:
            wx = _with_wx4py_info_suppressed(WeChatClient)
            _configure_wx4py_loggers()
            _with_wx4py_info_suppressed(wx.connect)
        except Exception as e:
            logger.warning(
                "wx4py connect failed ({}); replies disabled this run. "
                "Open WeChat's main window (not in tray) and restart dispatcher.", e,
            )
            return None
        return cls(wx)


# ---- wx4py UIA-direct (no mouse clicks) ---------------------------------


class UiaDirectReplier(Wx4pyReplier):
    """No-mouse WeChat 4.x sender built on wx4py's UI Automation controls.

    This backend never calls ``Click``, ``DoubleClick`` or wx4py's public
    ``send_to`` path.  It opens an exact ``session_item_<group>`` or unique
    exact search result through accessibility actions, verifies that exact
    session is selected, focuses the chat edit, writes via ValuePattern
    (clipboard as a keyboard-only fallback), and submits only while that edit
    still owns the keyboard focus.

    It is still desktop UI automation: Windows must be unlocked and WeChat's
    main window must exist.  The distinction is that the mouse pointer and
    screen coordinates are never used.
    """

    _SESSION_PREFIX = "session_item_"
    _SEARCH_ITEM_PREFIX = "search_item_"
    _CONTROL_WAIT_SECONDS = 0.45

    @classmethod
    def try_connect(cls) -> Replier | None:
        """Passively attach to an existing WeChat window.

        wx4py's ordinary ``connect`` repairs Narrator registry state, may
        restart WeChat, and may probe a login button.  Those side effects are
        appropriate for its regular backend but violate this backend's
        no-mouse contract.  Build the same UIA objects around the already-open
        HWND without changing registry or process state.
        """
        _configure_wx4py_loggers()
        try:
            from wx4py import WeChatClient
            from wx4py.core.uia_wrapper import UIAWrapper
            from wx4py.core.win32 import find_wechat_window
            from wx4py.features.chat import ChatWindow
        except ImportError:
            logger.warning("wx4py not installed; can't use uia-direct backend")
            return None

        try:
            hwnd = find_wechat_window()
            if not hwnd:
                logger.warning("uia-direct could not find an existing WeChat window")
                return None
            wx = WeChatClient(auto_connect=False)
            window = wx.window
            window._hwnd = hwnd
            window._uia = UIAWrapper(hwnd)
            window._initialized = True
            wx._chat_window = ChatWindow(window)
        except Exception as exc:
            logger.warning("uia-direct passive attach failed: {}", exc)
            return None
        return cls(wx)

    def _send_plain_group(self, group_name: str, body: str) -> None:
        chat = self._wx.chat_window
        if not self._open_exact_group_without_click(chat, group_name):
            raise ReplySendError(
                f"uia-direct could not select exact group {group_name!r} without a mouse click"
            )
        edit = self._prepare_input_without_click(chat)
        if edit is None:
            raise ReplySendError("uia-direct could not focus and clear the chat input")
        if not self._replace_text_without_click(chat, edit, body):
            self._clear_input(edit)
            raise ReplySendError("uia-direct could not write the reply into the chat input")
        if not self._exact_session_selected(chat, group_name):
            self._clear_input(edit)
            raise ReplySendError("uia-direct target session changed before submit")
        if not self._submit_focused_input_without_click(edit):
            self._clear_input(edit)
            raise ReplySendError("uia-direct refused Enter because the chat input lost focus")

    def _send_group_mention(self, group_name: str, requester: str, text: str) -> bool:
        """Compose a real @ token without invoking any mouse operation."""
        edit = None
        try:
            chat = self._wx.chat_window
            if not self._open_exact_group_without_click(chat, group_name):
                return False
            edit = self._prepare_input_without_click(chat)
            if edit is None:
                return False

            before_candidates = self._mention_candidate_signatures(chat.root, requester)
            if not self._control_has_keyboard_focus(edit):
                return False
            edit.SendKeys("@")
            time.sleep(0.2)
            if not chat.paste_text_into_focused_input(
                requester, log_error="failed to place @ requester on clipboard"
            ):
                self._clear_input(edit)
                return False
            time.sleep(0.5)
            after_candidates = self._mention_candidate_signatures(chat.root, requester)
            if not (after_candidates - before_candidates):
                logger.warning(
                    "uia-direct @ candidate not visible for requester={!r}", requester
                )
                self._clear_input(edit)
                return False

            if not self._control_has_keyboard_focus(edit):
                self._clear_input(edit)
                return False
            edit.SendKeys("{Enter}")
            time.sleep(0.2)

            suffix = text.strip()
            if suffix and not chat.paste_text_into_focused_input(
                _AT_SEP + suffix,
                log_error="failed to place @ reply body on clipboard",
            ):
                self._clear_input(edit)
                return False
            if not self._exact_session_selected(chat, group_name):
                self._clear_input(edit)
                return False
            if not self._submit_focused_input_without_click(edit):
                self._clear_input(edit)
                return False
            return True
        except Exception as exc:
            if edit is not None:
                self._clear_input(edit)
            logger.warning(
                "uia-direct real @ mention failed (group={!r}, requester={!r}); "
                "send will be refused: {}",
                group_name,
                requester,
                exc,
            )
            return False

    def probe_group(self, group_name: str) -> bool:
        """Select and verify one group without focusing input or sending text."""
        if not group_name or group_name not in settings.reply_allowed_groups:
            return False
        if os.name == "nt" and not _windows_input_desktop_available():
            return False
        return self._open_exact_group_without_click(self._wx.chat_window, group_name)

    def _open_exact_group_without_click(self, chat, group_name: str) -> bool:
        self._activate_chat_window(chat)
        if self._group_ready(chat, group_name):
            return True

        session = self._find_exact_session_control(chat.root, group_name)
        if session is not None and self._activate_and_verify_without_click(
            chat, group_name, session
        ):
            return True

        result = self._find_exact_search_result_without_click(chat, group_name)
        if result is None:
            return False
        return self._activate_and_verify_without_click(chat, group_name, result)

    @staticmethod
    def _activate_chat_window(chat) -> None:
        window = getattr(chat, "_window", None)
        if window is not None:
            window.activate()

    def _group_ready(self, chat, group_name: str) -> bool:
        if not self._exact_session_selected(chat, group_name):
            return False
        try:
            return chat._get_chat_input() is not None
        except Exception:
            return False

    def _find_exact_session_control(self, root, group_name: str):
        expected_id = self._SESSION_PREFIX + group_name
        try:
            control = root.ListItemControl(
                AutomationId=expected_id,
                searchDepth=20,
            )
            if not control.Exists(maxSearchSeconds=0.6):
                return None
            if (control.AutomationId or "") != expected_id:
                return None
            return control
        except Exception:
            return None

    def _exact_session_selected(self, chat, group_name: str) -> bool:
        control = self._find_exact_session_control(chat.root, group_name)
        if control is None:
            return False
        try:
            pattern = control.GetSelectionItemPattern()
            return bool(pattern and pattern.IsSelected)
        except Exception:
            return False

    def _activate_and_verify_without_click(
        self, chat, group_name: str, control
    ) -> bool:
        """Try semantic UIA actions, accepting only verified group selection.

        WeChat 4.1's Qt controls can return success from SelectionItemPattern
        or InvokePattern without changing the UI.  Treat every action result as
        a hint and re-check the exact selected session plus chat input before
        continuing.
        """
        actions = (
            self._legacy_default_action,
            self._selection_action,
            self._invoke_action,
            self._focused_enter_action,
        )
        for action in actions:
            if not action(control):
                continue
            time.sleep(self._CONTROL_WAIT_SECONDS)
            if self._group_ready(chat, group_name):
                return True
        return False

    @staticmethod
    def _legacy_default_action(control) -> bool:
        try:
            pattern = control.GetLegacyIAccessiblePattern()
            return bool(pattern and pattern.DoDefaultAction(waitTime=0))
        except Exception:
            return False

    @staticmethod
    def _selection_action(control) -> bool:
        try:
            pattern = control.GetSelectionItemPattern()
            return bool(pattern and pattern.Select())
        except Exception:
            return False

    @staticmethod
    def _invoke_action(control) -> bool:
        try:
            pattern = control.GetInvokePattern()
            return bool(pattern and pattern.Invoke())
        except Exception:
            return False

    @staticmethod
    def _focused_enter_action(control) -> bool:
        try:
            if not control.SetFocus():
                return False
            time.sleep(0.1)
            if not bool(control.HasKeyboardFocus):
                return False
            control.SendKeys("{Enter}")
            return True
        except Exception:
            return False

    def _find_exact_search_result_without_click(self, chat, group_name: str):
        try:
            search_edit = chat._get_search_edit(retries=1)
        except Exception:
            return None
        if search_edit is None:
            try:
                chat.root.SendKeys("{Ctrl}f")
            except Exception:
                return None
            time.sleep(0.3)
            try:
                search_edit = chat._get_search_edit(retries=1)
            except Exception:
                return None
        if search_edit is None:
            return None
        if not self._replace_text_without_click(chat, search_edit, group_name):
            return None
        time.sleep(0.8)
        popup = chat._get_search_popup()
        if popup is None:
            return None

        matches: list[object] = []

        def walk(control, depth: int) -> None:
            if depth > 15 or control is None:
                return
            try:
                name = (control.Name or "").strip()
                auto_id = control.AutomationId or ""
                if name == group_name and auto_id.startswith(self._SEARCH_ITEM_PREFIX):
                    matches.append(control)
            except Exception:
                pass
            try:
                children = control.GetChildren()
            except Exception:
                return
            for child in children:
                walk(child, depth + 1)

        walk(popup, 0)
        if len(matches) != 1:
            logger.warning(
                "uia-direct requires one exact search result for {!r}; found {}",
                group_name,
                len(matches),
            )
            return None
        return matches[0]

    def _prepare_input_without_click(self, chat):
        try:
            edit = chat._get_chat_input()
        except Exception:
            return None
        if edit is None or not self._focus_control_without_click(edit):
            return None
        if not self._clear_control_without_click(edit):
            return None
        return edit

    @staticmethod
    def _focus_control_without_click(control) -> bool:
        try:
            if not control.SetFocus():
                return False
            time.sleep(0.1)
            return bool(control.HasKeyboardFocus)
        except Exception:
            return False

    @staticmethod
    def _control_has_keyboard_focus(control) -> bool:
        try:
            return bool(control.HasKeyboardFocus)
        except Exception:
            return False

    @staticmethod
    def _value_pattern(control):
        try:
            pattern = control.GetValuePattern()
            if pattern and not pattern.IsReadOnly:
                return pattern
        except Exception:
            pass
        return None

    def _clear_control_without_click(self, control) -> bool:
        pattern = self._value_pattern(control)
        if pattern is not None:
            try:
                return bool(pattern.SetValue("") and pattern.Value == "")
            except Exception:
                pass
        if not self._control_has_keyboard_focus(control):
            return False
        try:
            control.SendKeys("{Ctrl}a")
            control.SendKeys("{Delete}")
            return True
        except Exception:
            return False

    def _replace_text_without_click(self, chat, control, text: str) -> bool:
        if not self._focus_control_without_click(control):
            return False
        pattern = self._value_pattern(control)
        if pattern is not None:
            try:
                if pattern.SetValue(text) and pattern.Value == text:
                    return self._control_has_keyboard_focus(control)
            except Exception:
                pass
        if not self._clear_control_without_click(control):
            return False
        try:
            pasted = chat.paste_text_into_focused_input(
                text,
                log_error="uia-direct clipboard write failed",
            )
        except Exception:
            return False
        return bool(pasted and self._control_has_keyboard_focus(control))

    def _submit_focused_input_without_click(self, edit) -> bool:
        if not self._control_has_keyboard_focus(edit):
            return False
        try:
            edit.SendKeys("{Enter}")
        except Exception:
            return False
        time.sleep(0.3)
        return True


# ---- factory --------------------------------------------------------------


def build_replier() -> Replier:
    """Build the configured replier. Always returns a working Replier (may
    be StdoutReplier if backend init failed)."""
    if not settings.reply:
        logger.info("WO_REPLY=False; using stdout replier")
        return StdoutReplier()

    backend = (settings.reply_backend or "wx4py").lower()
    if backend == "stdout":
        return StdoutReplier()
    if backend in {"wx4py", "uia-direct"}:
        if not settings.reply_allowed_groups:
            message = (
                "WO_REPLY_ALLOWED_GROUPS is empty; refusing to enable "
                f"{backend} sends"
            )
            if settings.reply_fail_closed:
                raise RuntimeError(message)
            logger.warning("{}; using stdout", message)
            return StdoutReplier()
        replier_cls = UiaDirectReplier if backend == "uia-direct" else Wx4pyReplier
        replier = replier_cls.try_connect()
        if replier is not None:
            return replier
        if settings.reply_fail_closed:
            raise RuntimeError(
                f"{backend} could not connect and WO_REPLY_FAIL_CLOSED is true"
            )
        return StdoutReplier()

    logger.warning(
        "unknown WO_REPLY_BACKEND={!r}; valid: wx4py / uia-direct / stdout. "
        "Using stdout.",
        backend,
    )
    return StdoutReplier()


def _windows_input_desktop_available() -> bool:
    """Return false on the secure/locked desktop; never attempt blind input."""
    if os.name != "nt":
        return True
    try:
        import ctypes
        user32 = ctypes.windll.user32
        desktop = user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_SWITCHDESKTOP
        if not desktop:
            return False
        try:
            return bool(user32.SwitchDesktop(desktop))
        finally:
            user32.CloseDesktop(desktop)
    except Exception:
        return False

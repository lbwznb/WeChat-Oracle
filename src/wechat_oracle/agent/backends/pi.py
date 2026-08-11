"""Text-only Pi RPC backend using Pi's existing local provider login."""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from loguru import logger

from ... import prompts
from ...config import settings
from ...db import transaction
from ...log_utils import dump_llm_call
from ..backend import AgentChatOutcome
from ..memory import insert_run_log
from ..orchestrator import _fetch_recent_for_agent, _format_recent_for_agent, _format_trace_for_log
from ..persona import assemble_system_prompts
from ..tools_write import phase_b_system_prompt

if TYPE_CHECKING:
    from ...dispatcher import CommandContext


@dataclass
class PiBackend:
    """One isolated no-tools Pi completion per chat trigger."""

    name: str = "pi"

    def chat(
        self,
        *,
        ctx: "CommandContext",
        user_question: str,
        trigger_kind: str,
        reflection_enabled: bool | None = None,
    ) -> AgentChatOutcome:
        del reflection_enabled
        started_at = time.time()
        recent_rows = _fetch_recent_for_agent(
            ctx.conn, ctx.group_id, settings.agent_recent_context_chat
        )
        recent_block = _format_recent_for_agent(recent_rows, bot_wxid=ctx.bot_wxid)
        system_prompt, _ = assemble_system_prompts(
            conn=ctx.conn,
            group_id=ctx.group_id,
            group_name=ctx.group_name,
            bot_name=ctx.bot_name,
            personas_dir=settings.agent_personas_dir,
            base_phase_b_prompt=phase_b_system_prompt(),
        )
        system_prompt += (
            "\n\n当前运行在只读、无工具的 Pi RPC 模式。请仅根据下面提供的群聊上下文回答；"
            "不要声称调用过工具，不要编造未提供的聊天记录。"
        )
        requester_line = (
            prompts.CHAT_REQUESTER_LINE.format(
                requester=ctx.requester, requester_repr=repr(ctx.requester)
            ) if ctx.requester else ""
        )
        quoted_line = (
            prompts.CHAT_QUOTED_LINE.format(quoted=ctx.quoted_text.strip())
            if ctx.quoted_text and ctx.quoted_text.strip() else ""
        )
        user_msg = prompts.CHAT_USER.format(
            now=datetime.now().strftime("%Y-%m-%d %H:%M"),
            trigger_line=prompts.CHAT_TRIGGER_LINE.format(
                trigger_kind=trigger_kind, trigger_msg_id=ctx.trigger_msg_id
            ),
            requester_line=requester_line,
            quoted_line=quoted_line,
            self_hint=prompts.CHAT_SELF_HINT.format(bot_wxid=ctx.bot_wxid) if ctx.bot_wxid else "",
            recent_block=recent_block,
            user_question=user_question,
        )
        reply = ctx.llm.complete_text(
            model=settings.pi_model,
            system=system_prompt,
            user=user_msg,
            temperature=0.4,
            max_tokens=settings.chat_max_tokens,
        ).strip()
        finished_at = time.time()
        trace = [{
            "step": 0,
            "kind": "pi_rpc_call",
            "tool": "_pi_rpc",
            "args": {"provider": settings.pi_provider, "model": settings.pi_model, "trigger_kind": trigger_kind},
            "result": reply or "(empty / silent)",
        }]
        try:
            with transaction(ctx.conn):
                run_id = insert_run_log(
                    ctx.conn,
                    group_id=ctx.group_id,
                    trigger_msg_id=ctx.trigger_msg_id,
                    trigger_kind=trigger_kind,
                    phase_a_trace=trace,
                    phase_b_trace=[],
                    reply_text=reply or None,
                    started_at=started_at,
                    finished_at=finished_at,
                )
        except Exception:
            logger.exception("pi: failed to write agent_run_log; reply still returned")
            run_id = None
        if ctx.llm_log_path:
            dump_llm_call(
                ctx.llm_log_path,
                label=f"pi-agent :: {user_question[:60]}",
                system=system_prompt,
                user=user_msg,
                raw=reply or "(silent)",
                parsed={"phase_a_trace": trace},
            )
        return AgentChatOutcome(
            reply_text=reply or None,
            trace_block=_format_trace_for_log(trace, []),
            run_id=run_id,
        )

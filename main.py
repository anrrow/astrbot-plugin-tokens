import json
import time
from collections import defaultdict
from datetime import datetime
from typing import Any

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Star, register

PLUGIN_NAME = "astrbot_plugin_token_inspector"
PLUGIN_VERSION = "1.2.0"

DEFAULT_CONFIG = {
    "history_size": 80,
    "top_items_limit": 12,
    "preview_chars": 160,
    "warning_enabled": True,
    "warning_threshold": 100000,
    "warning_cooldown_seconds": 600,
    "tool_definition_warn_threshold": 10000,
    "extra_parts_warn_threshold": 8000,
    "history_warn_threshold": 40000,
    "auto_trim_enabled": False,
    "auto_trim_threshold": 120000,
    "auto_trim_target": 90000,
    "auto_trim_keep_recent_contexts": 12,
    "notify_on_auto_trim": True,
    "show_preview_in_warning": True,
    "show_diagnosis_after_warning": True,
}

_ENCODER = None
_ENCODER_READY = False


def _cfg(config: Any, key: str) -> Any:
    """读取 AstrBotConfig / dict 配置，缺省使用 DEFAULT_CONFIG。"""
    try:
        if config is not None and key in config:
            return config[key]
    except Exception:
        pass
    return DEFAULT_CONFIG[key]


def _safe_int(value: Any, default: int, minimum: int | None = None) -> int:
    try:
        value = int(value)
    except Exception:
        value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


def _safe_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "on", "开启", "启用")
    if value is None:
        return default
    return bool(value)


def count_tokens(text: Any) -> int:
    """估算 token 数，优先 tiktoken；没有 tiktoken 时使用保守字符估算。"""
    if text is None:
        return 0
    if not isinstance(text, str):
        text = str(text)
    if not text:
        return 0

    global _ENCODER, _ENCODER_READY
    if not _ENCODER_READY:
        try:
            import tiktoken

            _ENCODER = tiktoken.get_encoding("cl100k_base")
        except Exception:
            _ENCODER = None
        _ENCODER_READY = True

    if _ENCODER is not None:
        try:
            return len(_ENCODER.encode(text))
        except Exception:
            pass

    # fallback：中文通常更接近 1~2 字/token，英文更接近 3~4 字/token；这里取偏保守估算。
    return max(1, len(text) // 2)


def safe_serialize(obj: Any, max_chars: int | None = None) -> str:
    """尽量转成稳定文本，避免对象 repr 失败。"""
    try:
        text = json.dumps(obj, ensure_ascii=False, default=lambda o: getattr(o, "__dict__", str(o)))
    except Exception:
        try:
            text = str(obj)
        except Exception:
            text = "<unserializable>"
    if max_chars is not None and len(text) > max_chars:
        return text[:max_chars] + "…"
    return text


def content_to_text(content: Any) -> str:
    """兼容 OpenAI-style 多模态 content、AstrBot TextPart、普通文本。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for part in content:
            if isinstance(part, str):
                chunks.append(part)
            elif isinstance(part, dict):
                # OpenAI/Anthropic 常见文本块
                if "text" in part:
                    chunks.append(str(part.get("text") or ""))
                elif part.get("type") == "text" and "content" in part:
                    chunks.append(str(part.get("content") or ""))
                else:
                    # 图片/文件等没有直接文本，但元数据也可能进入请求，这里保留方便定位。
                    chunks.append(safe_serialize(part))
            else:
                text = getattr(part, "text", None)
                chunks.append(str(text) if text is not None else safe_serialize(part))
        return "\n".join(x for x in chunks if x)
    text = getattr(content, "text", None)
    if text is not None:
        return str(text)
    return safe_serialize(content)


def preview(text: Any, limit: int) -> str:
    text = content_to_text(text).replace("\r", " ").replace("\n", " ").strip()
    text = " ".join(text.split())
    if len(text) > limit:
        return text[:limit] + "…"
    return text


def _add_item(items: list[dict], category: str, label: str, tokens: int, text: Any, preview_chars: int, meta: dict | None = None):
    if tokens <= 0:
        return
    item = {
        "category": category,
        "label": label,
        "tokens": tokens,
        "preview": preview(text, preview_chars),
    }
    if meta:
        item.update(meta)
    items.append(item)


def _iter_tool_candidates(func_tool: Any) -> list[tuple[str, Any]]:
    """尽量拆出每个工具定义，拆不开则作为一个整体。"""
    if not func_tool:
        return []

    candidates: Any = None
    if isinstance(func_tool, dict):
        for key in ("tools", "functions", "func_tools", "function_tools"):
            if key in func_tool:
                candidates = func_tool[key]
                break
        if candidates is None:
            candidates = func_tool
    else:
        for key in ("tools", "functions", "func_tools", "function_tools", "_tools"):
            try:
                value = getattr(func_tool, key)
                if value:
                    candidates = value
                    break
            except Exception:
                pass
        if candidates is None:
            try:
                candidates = list(func_tool)
            except Exception:
                candidates = None

    if candidates is None:
        return [("func_tool", func_tool)]

    result: list[tuple[str, Any]] = []
    if isinstance(candidates, dict):
        iterable = candidates.items()
    else:
        try:
            iterable = enumerate(candidates)
        except Exception:
            return [("func_tool", func_tool)]

    for key, tool in iterable:
        name = None
        if isinstance(tool, dict):
            name = tool.get("name") or tool.get("function", {}).get("name")
        if not name:
            name = getattr(tool, "name", None) or getattr(tool, "func_name", None) or str(key)
        result.append((str(name), tool))
    return result or [("func_tool", func_tool)]


def analyze_request(req: ProviderRequest, *, top_limit: int = 12, preview_chars: int = 160) -> dict:
    """分析 ProviderRequest 各部分 token 占用，并给出最大嫌疑项。"""
    result = {
        "system_prompt": 0,
        "current_prompt": 0,
        "history_user": 0,
        "history_assistant": 0,
        "history_tool": 0,
        "history_other": 0,
        "extra_parts": 0,
        "tool_definitions": 0,
        "tool_results": 0,
    }
    items: list[dict] = []

    system_prompt = getattr(req, "system_prompt", None) or ""
    system_tokens = count_tokens(system_prompt)
    result["system_prompt"] = system_tokens
    _add_item(items, "system_prompt", "系统提示 system_prompt", system_tokens, system_prompt, preview_chars)

    current_prompt = getattr(req, "prompt", None) or ""
    current_tokens = count_tokens(current_prompt)
    result["current_prompt"] = current_tokens
    _add_item(items, "current_prompt", "当前输入 prompt", current_tokens, current_prompt, preview_chars)

    history_by_role = defaultdict(int)
    contexts = getattr(req, "contexts", None) or []
    for idx, msg in enumerate(contexts):
        if isinstance(msg, dict):
            role = str(msg.get("role", "unknown"))
            content = msg.get("content", "")
        else:
            role = str(getattr(msg, "role", "unknown"))
            content = getattr(msg, "content", msg)
        text = content_to_text(content)
        tokens = count_tokens(text)
        history_by_role[role] += tokens
        _add_item(items, f"history_{role}", f"历史 #{idx + 1} / {role}", tokens, text, preview_chars, {"index": idx + 1, "role": role})

    result["history_user"] = history_by_role.get("user", 0)
    result["history_assistant"] = history_by_role.get("assistant", 0)
    result["history_tool"] = history_by_role.get("tool", 0) + history_by_role.get("function", 0)
    result["history_other"] = sum(v for k, v in history_by_role.items() if k not in ("user", "assistant", "tool", "function"))

    for idx, part in enumerate(getattr(req, "extra_user_content_parts", None) or []):
        text = content_to_text(part)
        tokens = count_tokens(text)
        result["extra_parts"] += tokens
        _add_item(items, "extra_parts", f"插件注入 extra_user_content_parts #{idx + 1}", tokens, text, preview_chars, {"index": idx + 1})

    func_tool = getattr(req, "func_tool", None)
    tool_candidates = _iter_tool_candidates(func_tool)
    if tool_candidates:
        whole_tool_text = safe_serialize(func_tool)
        result["tool_definitions"] = count_tokens(whole_tool_text)
        # 如果能拆开，就把单个工具也列出来；总量使用整体序列化值，避免漏算 ToolSet 外层结构。
        for name, tool in tool_candidates:
            tool_text = safe_serialize(tool)
            tokens = count_tokens(tool_text)
            _add_item(items, "tool_definitions", f"工具定义 {name}", tokens, tool_text, preview_chars, {"tool_name": name})
        if len(tool_candidates) == 1 and tool_candidates[0][0] == "func_tool" and items:
            items[-1]["tokens"] = result["tool_definitions"]

    tcr = getattr(req, "tool_calls_result", None)
    if tcr:
        tcr_text = safe_serialize(tcr)
        result["tool_results"] = count_tokens(tcr_text)
        _add_item(items, "tool_results", "本轮工具结果 tool_calls_result", result["tool_results"], tcr_text, preview_chars)

    result["total"] = sum(result.values())
    result["top_items"] = sorted(items, key=lambda x: x.get("tokens", 0), reverse=True)[:top_limit]
    result["advice"] = make_advice(result)
    return result


def make_advice(stats: dict) -> list[str]:
    total = max(1, int(stats.get("total") or 0))
    advice: list[str] = []

    if stats.get("tool_definitions", 0) > 10000 or stats.get("tool_definitions", 0) / total >= 0.25:
        advice.append("工具定义很重：优先关掉不用的 Agent 工具/MCP/插件工具，或者把长参数说明改短。")
    if (stats.get("history_user", 0) + stats.get("history_assistant", 0)) / total >= 0.45:
        advice.append("历史对话占比高：降低最大携带轮数，开启上下文压缩，或定期 /reset 当前会话。")
    if stats.get("extra_parts", 0) / total >= 0.20 or stats.get("extra_parts", 0) > 8000:
        advice.append("插件注入占比高：检查世界书/状态栏/长期记忆，只注入本轮相关内容，并尽量 mark_as_temp。")
    if stats.get("tool_results", 0) + stats.get("history_tool", 0) > 8000:
        advice.append("工具结果偏大：让工具返回摘要、分页或限制条数，不要把完整网页/日志/JSON 全塞回上下文。")
    if stats.get("system_prompt", 0) > 20000:
        advice.append("系统提示过长：把稳定设定拆短，动态状态不要反复追加到 system_prompt。")

    if not advice:
        advice.append("暂时没有明显异常；继续看 /token_top 里的单条最大项。")
    return advice[:4]


def get_session_id(event: AstrMessageEvent) -> str:
    try:
        return str(event.unified_msg_origin)
    except Exception:
        return str(getattr(event, "unified_msg_origin", "unknown"))


def fmt_num(n: Any) -> str:
    try:
        return f"{int(n):,}"
    except Exception:
        return str(n)


def pct(v: int, total: int) -> str:
    return f"{v / total * 100:.1f}%" if total > 0 else "0.0%"


def bar(v: int, total: int, width: int = 12) -> str:
    if total <= 0:
        return "░" * width
    filled = max(0, min(width, round(v / total * width)))
    return "█" * filled + "░" * (width - filled)


def level_text(total: int) -> str:
    if total >= 100000:
        return "🔴 爆表"
    if total >= 70000:
        return "🟠 偏高"
    if total >= 40000:
        return "🟡 注意"
    return "🟢 正常"


def format_stats_message(snap: dict, detail: bool = False) -> str:
    total = int(snap.get("total") or 0)
    rows = [
        ("🔧 系统提示", "system_prompt"),
        ("💬 当前输入", "current_prompt"),
        ("📜 历史-用户", "history_user"),
        ("🤖 历史-助手", "history_assistant"),
        ("🛠 工具历史", "history_tool"),
        ("🧩 插件注入", "extra_parts"),
        ("📦 工具定义", "tool_definitions"),
        ("🧾 本轮工具结果", "tool_results"),
        ("❓ 其他历史", "history_other"),
    ]

    lines = [
        "📊 Token Inspector｜最近一次 LLM 请求",
        "─" * 34,
        f"状态：{level_text(total)}",
        f"总计：{fmt_num(total)} tokens",
    ]

    if snap.get("trimmed"):
        lines.append(f"已自动裁剪历史：-{fmt_num(snap.get('trim_removed_tokens', 0))} tokens / {snap.get('trim_removed_count', 0)} 条")

    lines.append("─" * 34)
    for label, key in rows:
        value = int(snap.get(key) or 0)
        if value <= 0 and not detail:
            continue
        lines.append(f"{label:<9} {fmt_num(value):>8}  {bar(value, total)} {pct(value, total)}")

    lines.append("─" * 34)
    lines.append("最大嫌疑：")
    for i, item in enumerate((snap.get("top_items") or [])[:5], 1):
        lines.append(f"{i}. {item.get('label')}：{fmt_num(item.get('tokens', 0))} tokens")
        pv = item.get("preview") or ""
        if pv:
            lines.append(f"   ↳ {pv}")

    advice = snap.get("advice") or []
    if advice:
        lines.append("─" * 34)
        lines.extend([f"建议：{x}" for x in advice[:3]])

    lines.append("\n用 /token_top 看更详细的单条排行；用 /token_config 看当前配置。")
    return "\n".join(lines)


def format_top_message(snap: dict) -> str:
    total = int(snap.get("total") or 0)
    lines = [
        "🔎 Token 最大嫌疑排行",
        "─" * 34,
        f"总计：{fmt_num(total)} tokens｜时间：{snap.get('time', '-')}",
    ]
    for i, item in enumerate((snap.get("top_items") or [])[:12], 1):
        lines.append("─" * 34)
        lines.append(f"{i}. {item.get('label')}｜{fmt_num(item.get('tokens', 0))} tokens｜{pct(int(item.get('tokens') or 0), total)}")
        pv = item.get("preview") or ""
        if pv:
            lines.append(f"{pv}")
    return "\n".join(lines)


def format_diagnosis_message(snap: dict, config: Any) -> str:
    total = int(snap.get("total") or 0)
    tool_warn = _safe_int(_cfg(config, "tool_definition_warn_threshold"), 10000, minimum=1)
    extra_warn = _safe_int(_cfg(config, "extra_parts_warn_threshold"), 8000, minimum=1)
    history_warn = _safe_int(_cfg(config, "history_warn_threshold"), 40000, minimum=1)

    history_total = int(snap.get("history_user", 0)) + int(snap.get("history_assistant", 0)) + int(snap.get("history_tool", 0)) + int(snap.get("history_other", 0))
    checks = [
        ("工具定义 / MCP / 插件工具", int(snap.get("tool_definitions", 0)), tool_warn, "关掉不用的 Agent 工具、MCP、插件工具；工具参数说明不要写太长。"),
        ("世界书 / 状态栏 / 长期记忆注入", int(snap.get("extra_parts", 0)), extra_warn, "只注入本轮提到的角色/条目；超过 N 轮没触发就移除；临时内容尽量 mark_as_temp。"),
        ("历史上下文", history_total, history_warn, "降低最大携带轮数，开启 AstrBot 上下文压缩，或定期清空会话。"),
        ("系统提示", int(snap.get("system_prompt", 0)), 20000, "稳定设定压缩，动态状态不要每轮追加到 system_prompt。"),
        ("工具返回结果", int(snap.get("tool_results", 0)) + int(snap.get("history_tool", 0)), 8000, "工具返回摘要/分页/限制条数，不要塞完整网页、日志、JSON。"),
    ]

    lines = [
        "🧪 Token 诊断结论",
        "─" * 34,
        f"状态：{level_text(total)}｜总计：{fmt_num(total)} tokens",
    ]
    for name, value, threshold, suggestion in sorted(checks, key=lambda x: x[1], reverse=True):
        mark = "✅" if value < threshold else "⚠️"
        lines.append("─" * 34)
        lines.append(f"{mark} {name}：{fmt_num(value)} / {fmt_num(threshold)}")
        lines.append(f"处理：{suggestion}")
    return "\n".join(lines)


def format_config_message(config: Any) -> str:
    keys = [
        ("warning_enabled", "自动预警"),
        ("warning_threshold", "预警阈值"),
        ("warning_cooldown_seconds", "预警冷却秒数"),
        ("history_size", "每会话保留统计条数"),
        ("top_items_limit", "最大嫌疑排行数量"),
        ("preview_chars", "预览文本长度"),
        ("auto_trim_enabled", "自动裁剪"),
        ("auto_trim_threshold", "自动裁剪触发阈值"),
        ("auto_trim_target", "自动裁剪目标"),
        ("auto_trim_keep_recent_contexts", "裁剪时保留最近历史条数"),
    ]
    lines = ["⚙️ Token Inspector 当前配置", "─" * 34]
    for key, label in keys:
        lines.append(f"{label}：{_cfg(config, key)}")
    lines.append("\n这些配置在 AstrBot 自带 WebUI 的插件配置页修改，不需要单独开端口。")
    return "\n".join(lines)


def trim_contexts_if_needed(req: ProviderRequest, before_stats: dict, config: Any) -> dict | None:
    if not _safe_bool(_cfg(config, "auto_trim_enabled"), False):
        return None

    total = int(before_stats.get("total") or 0)
    threshold = _safe_int(_cfg(config, "auto_trim_threshold"), 120000, minimum=1)
    target = _safe_int(_cfg(config, "auto_trim_target"), 90000, minimum=1)
    keep_recent = _safe_int(_cfg(config, "auto_trim_keep_recent_contexts"), 12, minimum=0)
    preview_chars = _safe_int(_cfg(config, "preview_chars"), 160, minimum=20)
    top_limit = _safe_int(_cfg(config, "top_items_limit"), 12, minimum=3)

    if total < threshold:
        return None
    contexts = list(getattr(req, "contexts", None) or [])
    if not contexts:
        return None

    tail = contexts[-keep_recent:] if keep_recent else []
    head = contexts[:-keep_recent] if keep_recent else contexts[:]
    if not head:
        return None

    removed: list[dict] = []
    while head:
        current_contexts = head + tail
        try:
            req.contexts = current_contexts
        except Exception:
            return None
        now_stats = analyze_request(req, top_limit=top_limit, preview_chars=preview_chars)
        if int(now_stats.get("total") or 0) <= target:
            break
        msg = head.pop(0)
        if isinstance(msg, dict):
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
        else:
            role = getattr(msg, "role", "unknown")
            content = getattr(msg, "content", msg)
        removed.append({
            "role": str(role),
            "tokens": count_tokens(content_to_text(content)),
            "preview": preview(content, preview_chars),
        })

    try:
        req.contexts = head + tail
    except Exception:
        return None

    if not removed:
        return None
    return {
        "removed_count": len(removed),
        "removed_tokens": sum(x.get("tokens", 0) for x in removed),
        "removed_preview": removed[:5],
    }


_stats_history: dict[str, list] = defaultdict(list)
_latest_stats: dict[str, dict] = {}
_last_warn_at: dict[str, float] = defaultdict(float)


@register(
    PLUGIN_NAME,
    "Anrrow / optimized by ChatGPT",
    "Token 用量诊断插件：定位 token 来源、自动预警、QQ 指令诊断、可选历史裁剪",
    PLUGIN_VERSION,
    "https://github.com/anrrow2002-ctrl/astrbot_plugin_token_inspector",
)
class TokenInspector(Star):
    def __init__(self, context, config=None):
        super().__init__(context)
        self.config = config or {}

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        session_id = get_session_id(event)
        try:
            top_limit = _safe_int(_cfg(self.config, "top_items_limit"), 12, minimum=3)
            preview_chars = _safe_int(_cfg(self.config, "preview_chars"), 160, minimum=20)
            history_size = _safe_int(_cfg(self.config, "history_size"), 80, minimum=5)

            before_stats = analyze_request(req, top_limit=top_limit, preview_chars=preview_chars)
            before_total = int(before_stats.get("total") or 0)

            trim_info = trim_contexts_if_needed(req, before_stats, self.config)
            if trim_info:
                stats = analyze_request(req, top_limit=top_limit, preview_chars=preview_chars)
            else:
                stats = before_stats

            snapshot = {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "session": session_id,
                "before_total": before_total,
                **stats,
            }
            if trim_info:
                snapshot.update({
                    "trimmed": True,
                    "trim_removed_count": trim_info.get("removed_count", 0),
                    "trim_removed_tokens": trim_info.get("removed_tokens", 0),
                    "trim_removed_preview": trim_info.get("removed_preview", []),
                })

            _latest_stats[session_id] = snapshot
            _stats_history[session_id].append(snapshot)
            if len(_stats_history[session_id]) > history_size:
                _stats_history[session_id] = _stats_history[session_id][-history_size:]

            await self._maybe_send_warning(event, session_id, snapshot)
        except Exception as e:
            self.context.logger.warning(f"[token_inspector] 分析失败: {e}")

    async def _maybe_send_warning(self, event: AstrMessageEvent, session_id: str, snap: dict):
        if not _safe_bool(_cfg(self.config, "warning_enabled"), True):
            return
        threshold = _safe_int(_cfg(self.config, "warning_threshold"), 100000, minimum=1)
        cooldown = _safe_int(_cfg(self.config, "warning_cooldown_seconds"), 600, minimum=0)
        total = int(snap.get("total") or 0)
        before_total = int(snap.get("before_total") or total)
        hit_total = max(total, before_total)
        if hit_total < threshold:
            return
        now = time.time()
        if cooldown > 0 and now - _last_warn_at[session_id] < cooldown:
            return
        _last_warn_at[session_id] = now

        show_preview = _safe_bool(_cfg(self.config, "show_preview_in_warning"), True)
        top = (snap.get("top_items") or [])[:3]
        top_lines = []
        for i, item in enumerate(top, 1):
            line = f"{i}. {item.get('label')}：{fmt_num(item.get('tokens', 0))} tokens"
            if show_preview and item.get("preview"):
                line += f"\n   ↳ {item.get('preview')}"
            top_lines.append(line)

        msg = [
            f"⚠️ Token 已超过 {fmt_num(threshold)}，请检查。",
            f"当前请求：{fmt_num(total)} tokens" + (f"（裁剪前 {fmt_num(before_total)}）" if snap.get("trimmed") else ""),
        ]
        if top_lines:
            msg.append("最大来源：")
            msg.extend(top_lines)
        if snap.get("advice") and _safe_bool(_cfg(self.config, "show_diagnosis_after_warning"), True):
            msg.append("建议：" + snap["advice"][0])
        msg.append("发送 /token_stats、/token_top 或 /token_why 查看详情。")

        try:
            await event.send(event.plain_result("\n".join(msg)))
        except Exception as e:
            self.context.logger.warning(f"[token_inspector] 预警发送失败: {e}")

    @filter.command("token_stats")
    async def token_stats(self, event: AstrMessageEvent):
        session_id = get_session_id(event)
        snap = _latest_stats.get(session_id)
        if not snap:
            yield event.plain_result("⚠️ 还没有捕获到本会话的 LLM 请求，先和 AI 对话一轮再试。")
            return
        yield event.plain_result(format_stats_message(snap))

    @filter.command("token_top")
    async def token_top(self, event: AstrMessageEvent):
        session_id = get_session_id(event)
        snap = _latest_stats.get(session_id)
        if not snap:
            yield event.plain_result("⚠️ 还没有捕获到本会话的 LLM 请求，先和 AI 对话一轮再试。")
            return
        yield event.plain_result(format_top_message(snap))

    @filter.command("token_why")
    async def token_why(self, event: AstrMessageEvent):
        session_id = get_session_id(event)
        snap = _latest_stats.get(session_id)
        if not snap:
            yield event.plain_result("⚠️ 还没有捕获到本会话的 LLM 请求，先和 AI 对话一轮再试。")
            return
        yield event.plain_result(format_diagnosis_message(snap, self.config))

    @filter.command("token_sessions")
    async def token_sessions(self, event: AstrMessageEvent):
        if not _latest_stats:
            yield event.plain_result("暂无 token 统计。")
            return
        entries = sorted(_latest_stats.values(), key=lambda x: int(x.get("total") or 0), reverse=True)[:20]
        lines = ["📚 Token 会话总览", "─" * 34]
        for i, snap in enumerate(entries, 1):
            sid = snap.get("session", "unknown")
            if len(sid) > 42:
                sid = sid[:39] + "…"
            lines.append(f"{i}. {fmt_num(snap.get('total', 0)):>8} tokens｜{sid}｜{snap.get('time', '-')}")
        yield event.plain_result("\n".join(lines))

    @filter.command("token_config")
    async def token_config(self, event: AstrMessageEvent):
        yield event.plain_result(format_config_message(self.config))

    @filter.command("token_reset")
    async def token_reset(self, event: AstrMessageEvent):
        session_id = get_session_id(event)
        _latest_stats.pop(session_id, None)
        _stats_history.pop(session_id, None)
        _last_warn_at.pop(session_id, None)
        yield event.plain_result("✅ 已清空本会话的 token 统计记录。")

    @filter.command("token_help")
    async def token_help(self, event: AstrMessageEvent):
        yield event.plain_result(
            "Token Inspector 指令：\n"
            "/token_stats：查看当前会话最近一次 token 分布\n"
            "/token_top：查看单条最大嫌疑排行\n"
            "/token_why：按工具/插件注入/历史/系统提示给诊断结论\n"
            "/token_sessions：查看所有会话 token 排行\n"
            "/token_config：查看当前插件配置\n"
            "/token_reset：清空本会话统计\n\n"
            "超过 warning_threshold（默认 100,000）会自动预警；配置在 AstrBot 自带 WebUI 的插件配置页修改，不需要独立 Web 面板。"
        )

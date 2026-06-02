import json
import asyncio
from aiohttp import web
from datetime import datetime
from collections import defaultdict

from astrbot.api.star import Star, register
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.provider import ProviderRequest

PANEL_PORT = 7800


def count_tokens(text: str) -> int:
    """估算 token 数，优先用 tiktoken，fallback 到字符估算"""
    if not text:
        return 0
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(str(text)))
    except Exception:
        # fallback: 中文约1.5字/token，英文约4字/token，混合取3
        return max(1, len(str(text)) // 3)


def analyze_request(req: ProviderRequest) -> dict:
    """分析 ProviderRequest 各部分 token 占用"""
    result = {}

    # 1. 系统提示
    result["system_prompt"] = count_tokens(req.system_prompt or "")

    # 2. 当前用户输入
    result["current_prompt"] = count_tokens(req.prompt or "")

    # 3. 对话历史（按 role 拆分）
    history_by_role = defaultdict(int)
    for msg in (req.contexts or []):
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if isinstance(content, list):
            # 多模态 content
            text = " ".join(
                part.get("text", "") for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        else:
            text = str(content)
        history_by_role[role] += count_tokens(text)

    result["history_user"] = history_by_role.get("user", 0)
    result["history_assistant"] = history_by_role.get("assistant", 0)
    result["history_tool"] = history_by_role.get("tool", 0) + history_by_role.get("function", 0)
    result["history_other"] = sum(
        v for k, v in history_by_role.items()
        if k not in ("user", "assistant", "tool", "function")
    )

    # 4. 插件注入的 extra_user_content_parts
    extra = 0
    for part in (getattr(req, "extra_user_content_parts", None) or []):
        extra += count_tokens(str(part))
    result["extra_parts"] = extra

    # 5. Function tools 定义（Agent 模式的胖头鱼）
    tools_tokens = 0
    func_tool = getattr(req, "func_tool", None)
    if func_tool:
        tools_tokens = count_tokens(json.dumps(func_tool, ensure_ascii=False, default=str))
    result["tool_definitions"] = tools_tokens

    # 6. tool_calls_result
    tcr = getattr(req, "tool_calls_result", None)
    if tcr:
        result["tool_results"] = count_tokens(str(tcr))
    else:
        result["tool_results"] = 0

    result["total"] = sum(result.values())
    return result


# 全局统计存储: session_id -> list of snapshot
_stats_history: dict[str, list] = defaultdict(list)
_latest_stats: dict[str, dict] = {}  # session_id -> latest snapshot


@register(
    "astrbot_plugin_token_inspector",
    "Anrrow",
    "Token 用量分析插件：拦截每次 LLM 请求，统计各部分 token 占用",
    "1.0.0",
    "https://github.com/anrrow2002-ctrl/astrbot_plugin_token_inspector"
)
class TokenInspector(Star):

    def __init__(self, context):
        super().__init__(context)
        self._web_runner = None
        self._web_app = None
        # 启动 web panel
        asyncio.create_task(self._start_web_panel())

    # ========== LLM 请求钩子 ==========

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        try:
            stats = analyze_request(req)
            session_id = getattr(event, "unified_msg_origin", "unknown")
            snapshot = {
                "time": datetime.now().strftime("%H:%M:%S"),
                "session": session_id,
                **stats
            }
            _latest_stats[session_id] = snapshot
            _stats_history[session_id].append(snapshot)
            # 只保留最近 50 条
            if len(_stats_history[session_id]) > 50:
                _stats_history[session_id] = _stats_history[session_id][-50:]
        except Exception as e:
            self.context.logger.warning(f"[token_inspector] 分析失败: {e}")

    # ========== 指令：查看当前会话统计 ==========

    @filter.command("token_stats")
    async def token_stats(self, event: AstrMessageEvent):
        session_id = getattr(event, "unified_msg_origin", "unknown")
        snap = _latest_stats.get(session_id)
        if not snap:
            yield event.plain_result("⚠️ 还没有捕获到本会话的 LLM 请求，发一条消息再试。")
            return

        total = snap["total"]

        def pct(v):
            return f"{v / total * 100:.1f}%" if total > 0 else "0%"

        def bar(v, width=10):
            filled = int(v / total * width) if total > 0 else 0
            return "█" * filled + "░" * (width - filled)

        lines = [
            f"📊 Token 分布（最近一次请求）",
            f"{'─' * 30}",
            f"总计:          {total:>7,} tokens",
            f"{'─' * 30}",
            f"🔧 系统提示:    {snap['system_prompt']:>7,}  {bar(snap['system_prompt'])} {pct(snap['system_prompt'])}",
            f"💬 当前输入:    {snap['current_prompt']:>7,}  {bar(snap['current_prompt'])} {pct(snap['current_prompt'])}",
            f"📜 历史-用户:   {snap['history_user']:>7,}  {bar(snap['history_user'])} {pct(snap['history_user'])}",
            f"🤖 历史-助手:   {snap['history_assistant']:>7,}  {bar(snap['history_assistant'])} {pct(snap['history_assistant'])}",
            f"🛠️  工具结果:    {snap['history_tool']:>7,}  {bar(snap['history_tool'])} {pct(snap['history_tool'])}",
            f"💉 插件注入:    {snap['extra_parts']:>7,}  {bar(snap['extra_parts'])} {pct(snap['extra_parts'])}",
            f"📦 工具定义*:   {snap['tool_definitions']:>7,}  {bar(snap['tool_definitions'])} {pct(snap['tool_definitions'])}",
            f"{'─' * 30}",
            f"* 工具定义 = Agent 模式下所有注册工具的 schema",
            f"🌐 详细面板: http://your-vps:7800",
        ]
        yield event.plain_result("\n".join(lines))

    # ========== Web Panel ==========

    async def _start_web_panel(self):
        try:
            self._web_app = web.Application()
            self._web_app.router.add_get("/", self._handle_index)
            self._web_app.router.add_get("/api/stats", self._handle_api_stats)

            runner = web.AppRunner(self._web_app)
            await runner.setup()
            site = web.TCPSite(runner, "0.0.0.0", PANEL_PORT)
            await site.start()
            self._web_runner = runner
            self.context.logger.info(f"[token_inspector] Web panel 启动于 http://0.0.0.0:{PANEL_PORT}")
        except Exception as e:
            self.context.logger.warning(f"[token_inspector] Web panel 启动失败: {e}")

    async def _handle_api_stats(self, request: web.Request):
        data = {
            "latest": _latest_stats,
            "history": {k: v[-20:] for k, v in _stats_history.items()}
        }
        return web.json_response(data)

    async def _handle_index(self, request: web.Request):
        html = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Token Inspector</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'JetBrains Mono', 'Consolas', monospace; background: #0d1117; color: #e6edf3; padding: 24px; }
  h1 { color: #58a6ff; font-size: 1.4rem; margin-bottom: 20px; }
  h1 span { color: #8b949e; font-size: 0.9rem; font-weight: normal; margin-left: 8px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(420px, 1fr)); gap: 16px; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 20px; }
  .card h2 { font-size: 0.85rem; color: #8b949e; margin-bottom: 12px; word-break: break-all; }
  .total { font-size: 2rem; font-weight: bold; color: #58a6ff; margin-bottom: 16px; }
  .total small { font-size: 0.9rem; color: #8b949e; }
  .row { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; font-size: 0.85rem; }
  .row .label { width: 110px; color: #8b949e; flex-shrink: 0; }
  .row .val { width: 70px; text-align: right; flex-shrink: 0; }
  .row .bar-wrap { flex: 1; background: #21262d; border-radius: 4px; height: 8px; overflow: hidden; }
  .row .bar { height: 100%; border-radius: 4px; transition: width 0.4s; }
  .row .pct { width: 42px; text-align: right; color: #8b949e; flex-shrink: 0; }
  .c0 { background: #238636; }
  .c1 { background: #1f6feb; }
  .c2 { background: #9e6a03; }
  .c3 { background: #58a6ff; }
  .c4 { background: #f78166; }
  .c5 { background: #bc8cff; }
  .c6 { background: #ff7b72; }
  .time { font-size: 0.75rem; color: #6e7681; margin-top: 10px; }
  .alert { background: #3d1f1f; border: 1px solid #f85149; color: #f85149; border-radius: 6px; padding: 8px 12px; margin-bottom: 12px; font-size: 0.8rem; }
  .empty { color: #8b949e; text-align: center; padding: 40px; }
  .refresh { background: #21262d; border: 1px solid #30363d; color: #e6edf3; padding: 6px 14px; border-radius: 6px; cursor: pointer; font-size: 0.85rem; margin-bottom: 20px; }
  .refresh:hover { background: #30363d; }
</style>
</head>
<body>
<h1>🔍 Token Inspector <span>AstrBot Plugin</span></h1>
<button class="refresh" onclick="load()">↻ 刷新</button>
<div class="grid" id="grid"><div class="empty">加载中...</div></div>

<script>
const FIELDS = [
  { key: "system_prompt",     label: "系统提示",   cls: "c0" },
  { key: "current_prompt",    label: "当前输入",   cls: "c1" },
  { key: "history_user",      label: "历史·用户",  cls: "c2" },
  { key: "history_assistant", label: "历史·助手",  cls: "c3" },
  { key: "history_tool",      label: "工具结果",   cls: "c4" },
  { key: "extra_parts",       label: "插件注入",   cls: "c5" },
  { key: "tool_definitions",  label: "工具定义",   cls: "c6" },
];

function fmt(n) { return n.toLocaleString(); }

async function load() {
  const res = await fetch("/api/stats");
  const data = await res.json();
  const grid = document.getElementById("grid");
  const entries = Object.entries(data.latest || {});
  if (!entries.length) {
    grid.innerHTML = '<div class="empty">还没有捕获到任何请求，发一条消息试试 👀</div>';
    return;
  }
  grid.innerHTML = entries.map(([sid, snap]) => {
    const total = snap.total || 1;
    const toolPct = (snap.tool_definitions / total * 100).toFixed(1);
    const alert = snap.tool_definitions > 10000
      ? `<div class="alert">⚠️ 工具定义占 ${toolPct}% — Agent 模式工具过多！</div>` : "";
    const rows = FIELDS.map(f => {
      const v = snap[f.key] || 0;
      const pct = (v / total * 100).toFixed(1);
      return `<div class="row">
        <span class="label">${f.label}</span>
        <span class="val">${fmt(v)}</span>
        <div class="bar-wrap"><div class="bar ${f.cls}" style="width:${pct}%"></div></div>
        <span class="pct">${pct}%</span>
      </div>`;
    }).join("");
    return `<div class="card">
      <h2>${sid}</h2>
      <div class="total">${fmt(total)} <small>tokens</small></div>
      ${alert}
      ${rows}
      <div class="time">最近更新: ${snap.time}</div>
    </div>`;
  }).join("");
}

load();
setInterval(load, 5000);
</script>
</body>
</html>"""
        return web.Response(text=html, content_type="text/html")

    async def destroy(self):
        if self._web_runner:
            await self._web_runner.cleanup()

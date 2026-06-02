# astrbot_plugin_token_inspector

拦截每次 LLM 请求，分析各部分 token 占用，一键找出你的 token 胖头鱼。

## 功能

- 自动拦截每次 LLM 请求，按来源分类统计 token
- `/token_stats` 指令：直接在聊天里查看当前会话 token 分布
- Web Panel（`http://your-vps:7800`）：可视化看所有会话的实时 token 分布，工具定义占用过高自动红色警告

## Token 分类

| 分类 | 说明 |
|---|---|
| 系统提示 | `system_prompt` |
| 当前输入 | 本轮用户消息 |
| 历史·用户 | 历史对话中用户消息 |
| 历史·助手 | 历史对话中助手回复 |
| 工具结果 | tool call 返回内容 |
| 插件注入 | `extra_user_content_parts` |
| **工具定义** | **Agent 模式下所有注册工具的 schema（常见胖头鱼）** |

## 安装

1. 克隆到 AstrBot 插件目录
2. 重启 AstrBot 或热重载
3. 在 WebUI 安装 `tiktoken`（可选，无则用字符估算）

## 用法

```
/token_stats   # 查看当前会话最近一次请求的 token 分布
```

Web Panel 每 5 秒自动刷新，工具定义超过 10,000 token 会高亮警告。

## 注意

- Token 计数使用 `tiktoken cl100k_base` 估算，与实际模型可能有 ±5% 误差
- Web Panel 默认端口 7800，如有冲突修改 `main.py` 顶部的 `PANEL_PORT`

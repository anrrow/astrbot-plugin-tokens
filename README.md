# astrbot_plugin_token_inspector Lite / QQ 指令版

拦截 AstrBot 每次 LLM 请求，分析 token 来源，帮你找出“token 胖头鱼”。

## 这版改动

- 取消独立 Web 面板：不再启动 `aiohttp`，不占用端口，不需要 VPS / Docker 放行端口。
- 配置改走 AstrBot 自带 WebUI 插件配置页：通过 `_conf_schema.json` 显示可视化配置。
- 详情改走 QQ / 聊天指令输出：适合 QQ Bot 场景，不需要额外面板。
- 保留自动预警：总 token 超过 `100,000` 时，机器人会在当前 QQ/聊天会话发送提醒。
- 保留定位能力：能看出第几条历史、哪个工具定义、哪个插件注入、哪个工具结果最大。
- 新增 `/token_why`：按“工具定义 / 插件注入 / 历史上下文 / system prompt / 工具结果”给出诊断结论。
- 可选自动裁剪：默认关闭；开启后超过阈值会裁掉最早的历史上下文，保留最近 N 条。

## 指令

```text
/token_stats      查看当前会话最近一次 token 分布
/token_top        查看单条最大嫌疑排行
/token_why        查看 token 过大的诊断结论
/token_sessions   查看所有会话 token 排行
/token_config     查看当前插件配置
/token_reset      清空本会话统计
/token_help       查看帮助
```

## AstrBot 自带面板怎么用

这版没有自己的 Web 面板。配置请到 AstrBot WebUI：

```text
插件管理 / 配置页 -> astrbot_plugin_token_inspector -> 修改配置 -> 保存 -> 重载插件
```

可配置内容包括：

- `warning_enabled`：是否开启自动预警
- `warning_threshold`：总 token 预警阈值，默认 100000
- `warning_cooldown_seconds`：同一会话预警冷却时间
- `preview_chars`：QQ 指令里显示多少预览文本
- `show_preview_in_warning`：预警消息是否显示文本预览
- `auto_trim_enabled`：是否自动裁剪旧历史，默认关闭
- `auto_trim_threshold` / `auto_trim_target` / `auto_trim_keep_recent_contexts`：自动裁剪策略

## Token 分类

| 分类 | 说明 |
|---|---|
| 系统提示 | `req.system_prompt` |
| 当前输入 | `req.prompt` |
| 历史·用户 | `req.contexts` 中 role=user |
| 历史·助手 | `req.contexts` 中 role=assistant |
| 工具历史 | 历史中的 tool/function 消息 |
| 插件注入 | `req.extra_user_content_parts` |
| 工具定义 | Agent/MCP/插件工具 schema，即 `req.func_tool` |
| 本轮工具结果 | `req.tool_calls_result` |

## 安装

1. 将插件文件夹放入 AstrBot 插件目录。
2. 重启 AstrBot，或在 WebUI 里热重载插件。
3. 推荐安装 `tiktoken`：

```bash
pip install tiktoken
```

没有 `tiktoken` 也能运行，但会使用字符估算，误差更大。

## token 变大的常见原因

1. Agent 工具/MCP 太多：工具 schema 每轮都可能进请求。
2. 世界书/状态栏/长期记忆插件注入太多：看 `extra_parts`。
3. 历史对话太长：看 `history_user` 和 `history_assistant`。
4. 工具返回太大：网页全文、日志、JSON、搜索结果全塞进上下文。
5. system prompt 被动态追加：每轮变化的内容不要一直塞 system。

## 建议控制方案

- 开启 AstrBot 的上下文压缩或降低最大携带轮数。
- 不用的 Agent 工具、MCP、插件工具先关掉。
- 世界书只注入本轮相关角色/条目，不要“触发后一直注入”。
- 工具结果做摘要/分页，不要返回全文。
- 动态状态用 `extra_user_content_parts`，临时内容尽量 `mark_as_temp()`。
- 本插件的 `auto_trim_enabled` 默认关闭，确认可接受丢弃旧历史后再开。

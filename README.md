# 复读机与自主撤回（repeater-recall）

MaiBot 插件：经典复读机 + LLM 自主撤回。

## 功能

1. **复读机**：同一聊天流内连续 N 条相同文本后，bot 跟读一次，之后进入冷却期防止连环复读。默认要求接龙的是**不同用户**（同一个人连刷同一句不触发，A-B-A 交替也不触发）。纯数字、超短英文单词、命令消息、戳一戳/拍一拍系统通知不参与统计。
2. **出站消息全量记录**（v1.0.2 新增）：通过 `send_service.after_send` 只读钩子捕获**所有** bot 出站消息的 `platform_message_id`，包括 Maisaka 主链路的日常回复（这类消息不经过插件自己发送，v1.0.1 及以前记录不到）。
3. **发送后自评撤回**：bot 每条消息（含复读与主链路回复）发出后延迟数秒，由 LLM 自评是否明显不合适（易引战 / 严重答非所问 / 可能违规）。自评默认携带最近 10 条聊天记录做语境判断——接梗、玩谐音、延续当前话题不算跑题，避免「单看像跑题实际是正常回复」的误撤。判定"该撤"则自动通过 NapCat 撤回。每小时有撤回配额上限兜底防误撤。
4. **LLM 撤回工具**：注册 `recall_last_message` 工具，LLM 在对话中发现自己的回复不合适时可主动调用撤回。
5. **手动撤回命令（管理员专用）**：`/recall` 或 `/撤回` 撤回 bot 最近一条消息（2 分钟时限内）。触发者须在 `recall.admin_ids` 管理员列表中，本地控制台（bot_console）不受限制。

### 撤回目标的两级定位

| 级别 | 数据来源 | 覆盖场景 |
|---|---|---|
| 一级 | `send_service.after_send` 钩子记录的内存表 | 插件加载后所有 bot 出站消息（含主链路回复、复读跟读） |
| 二级 | `message.get_recent` + `get_login_info` 历史回溯 | 插件刚加载/重载前的消息、钩子未回填 ID 的兜底 |

两级都取不到才返回"没有可撤回的 bot 消息"。

## 依赖

- MaiBot Host >= 1.2.0，maibot-plugin-sdk >= 2.5.0
- **撤回功能硬依赖 `maibot-team.napcat-adapter` >= 1.4.0**（manifest 已声明 plugin 依赖，Host 会保证启动顺序）。适配器缺失时撤回操作报"适配器不可用"，复读功能不受影响。

## 安装与启用

1. 将 `repeater-recall/` 目录放入 MaiBot 的 `plugins/` 下。
2. 重启 MaiBot（manifest 含 capabilities 与插件依赖声明，必须完整重启，热重载不生效）。
3. WebUI → 插件管理中确认「复读机与自主撤回」已启用。
4. 确保 NapCat 已登录且 napcat-adapter 连接正常。
5. 在 WebUI 插件配置中把你的 QQ 号填入 `recall.admin_ids`（如 `["123456789"]`），否则 `/recall` 命令只有本地控制台能用。

## 配置项（config.toml，由 Runner 自动生成）

### [plugin]

| 字段 | 默认 | 说明 |
|---|---|---|
| enabled | true | 插件总开关 |
| config_version | "1" | 配置版本（勿改） |

### [repeat]

| 字段 | 默认 | 说明 |
|---|---|---|
| enabled | true | 复读机开关 |
| threshold | 3 | 连续相同消息达到几条后跟读（2–10） |
| cooldown_seconds | 300 | 跟读后冷却秒数（0–3600） |
| min_length | 2 | 参与统计的最短文本长度 |
| ignore_case | true | 比较时忽略大小写 |
| require_distinct_users | true | 是否要求接龙的必须是**不同用户**：开启后同一个人连刷同一句不触发，必须连续 N 条来自 N 个不同用户才跟读；关闭则退化为旧行为（只看文本连续重复）。取不到发送者 ID 时按「不同用户」处理，不会让复读机哑火 |

### [recall]

| 字段 | 默认 | 说明 |
|---|---|---|
| enabled | true | 自主撤回开关 |
| self_review | true | 发送后自动自评撤回 |
| review_delay_seconds | 8 | 发送后延迟几秒自评（1–120） |
| review_model | `""` | 自评用的模型名；留空用 Host 默认。Host 把本插件的 LLM 路由到 embedding 模型时，填具体模型名绕过（见 FAQ） |
| context_messages | 10 | 自评时带入的近期聊天记录条数，用于语境判断（接梗/玩谐音/回应点名不算跑题）；0 = 关闭语境感知，仅凭消息本身判断。历史接口异常时自动降级为纯文本判定，不影响撤回链路（0–50） |
| max_recalls_per_hour | 6 | 每小时撤回总次数上限（含自评/工具/手动命令）（1–60） |
| admin_ids | [] | 可用 `/recall`/`/撤回` 命令的管理员 QQ 列表，兼容 `["qq:123456789"]` 格式；为空时仅本地控制台（bot_console）可用。LLM 工具与自评撤回不受此列表限制 |

## 命令

| 命令 | 说明 |
|---|---|
| `/recall` 或 `/撤回` | **管理员专用**：手动撤回 bot 最近一条消息。触发者须在 `recall.admin_ids` 中，或来自本地控制台 |

## 测试方式（本地，无需真机）

```bash
# 在 MaiBot插件开发/ 目录下
.venv/Scripts/python.exe check_plugin.py plugins/repeater-recall   # 结构自检
.venv/Scripts/python.exe smoke_test.py plugins/repeater-recall     # 生命周期冒烟
.venv/Scripts/python.exe test_repeater_recall.py                   # 功能直调测试（72 项）
.venv/Scripts/python.exe run_gates.py plugins/repeater-recall      # check + smoke 双门禁
```

测试覆盖：装饰器-函数配对、复读触发/冷却/分流、过滤规则、撤回四态（成功/失败/适配器缺失/超时）、
配额、自评三态（该撤/不撤/垃圾响应）、命令鉴权五种身份、**after_send 记录五态**（主链路含负数
message_id / sent=False / 缺 ID / 非 bot / 登录信息不可用降级）、**历史兜底六态**（命中/无 bot 消息/
过期/缺时间戳/内存优先/接口故障静默）、**manifest 能力声明 + 退避熔断九态**（失败退避/退避期内不重试/
退避过期恢复/成功后缓存/自评熔断/熔断不误伤撤回/review_model 透传/默认空串/成功后清零）。

## 常见问题

- **撤回报"没有可撤回的 bot 消息"（v1.0.2 已修复主因）**：v1.0.1 只记录插件自己发出的消息，Maisaka 主链路的日常回复拿不到 `platform_message_id`。v1.0.2 增加 `send_service.after_send` 全量记录 + 历史回溯兜底。若仍报该提示，请查日志：
  - 出现 `[诊断] after_send 未取到平台消息 ID：stream=... keys=[...]` → 说明该 Host 版本的 `after_send` 载荷回填时机晚于钩子，把 `keys=` 里的字段列表贴出来；此时可先依赖历史回溯兜底。
  - 出现 `get_login_info 失败，历史兜底定位 bot 消息将不可用` → napcat-adapter 未连接，历史兜底失效，只剩钩子记录一条路。
- **撤回报"napcat 适配器不可用（RPCError / RPCError）"**（v1.0.3 已修）：日志里会同时出现
  `未获授权能力: api.call`。官方手册把 `api.call` 列为「免声明」，**实测 Host 1.2.3 仍会拒绝**——
  必须在 manifest 的 `capabilities` 里显式写上 `"api.call"`，改完**完整重启** MaiBot（热重载不生效）。
  v1.0.3 已补上，升级后若仍报此错，说明插件目录没换干净或没重启。
- **自评每天狂刷 `qwen3.7-text-embedding ... Field required: input.contents`**：Host 没给本插件的 LLM
  任务（`plugin.org.mai-mai.repeater-recall`）配文本生成模型，fallback 到了向量模型。三选一：
  1. 在 `model_config.toml` 里给任务 `plugin.org.mai-mai.repeater-recall` 配一个文本模型（根治）；
  2. 把插件配置 `recall.review_model` 填成具体模型名（绕过任务路由，改完热重载即可）；
  3. 把 `recall.self_review` 设为 `false` 彻底关闭自评。
  自评连续失败 3 次会自动熔断 10 分钟并停止刷屏，**撤回工具与 `/recall` 命令不受自评影响**。
- **一次回复被切成多段，撤回只撤掉最后一段**：这是 MaiBot 的智能分段机制，每段都是独立消息、
  各有自己的 `message_id`。插件只保留最近一条，需要全撤就连续调用几次撤回。
- **撤回报"适配器不可用"**：检查 napcat-adapter 是否加载、NapCat 是否在线；`ctx.api.call` 报「API 不存在」通常是适配器未装或版本 < 1.4.0。
- **撤回报超时限**：QQ 平台限制约 2 分钟，插件按 120 秒保守截断。
- **复读不触发**：确认文本长度 ≥ min_length、不是纯数字/超短英文单词、不在冷却期；`/命令` 消息不参与统计。
- **自评从不撤回**：LLM 判定偏保守属正常设计（仅明显不合适才撤）；可调低 `review_delay_seconds` 观察 WebUI/日志中自评记录。
- **命令提示"仅管理员可用"**：把触发者的 QQ 号加进 `recall.admin_ids`（WebUI 插件配置或 config.toml 均可，热重载生效），格式 `["123456789"]` 或 `["qq:123456789"]`；本地控制台始终可用。

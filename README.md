# 复读机与自主撤回（repeater-recall）

MaiBot 插件：经典复读机 + LLM 自主撤回。

## 功能

1. **复读机**：同一聊天流内连续 N 条相同文本后，bot 跟读一次，之后进入冷却期防止连环复读。默认要求接龙的是**不同用户**（同一个人连刷同一句不触发，A-B-A 交替也不触发）。纯数字、超短英文单词、命令消息、戳一戳/拍一拍系统通知不参与统计。
2. **出站消息全量记录**（v1.0.2 新增）：通过 `send_service.after_send` 只读钩子捕获**所有** bot 出站消息的 `platform_message_id`，包括 Maisaka 主链路的日常回复（这类消息不经过插件自己发送，v1.0.1 及以前记录不到）。
3. **发送后自评撤回**：bot 每条消息（含复读与主链路回复）发出后延迟数秒，由 LLM 自评是否明显不合适（易引战 / 严重答非所问 / 可能违规）。自评默认携带最近 10 条聊天记录做语境判断——接梗、玩谐音、延续当前话题不算跑题，避免「单看像跑题实际是正常回复」的误撤。判定"该撤"则自动通过 NapCat 撤回。每小时有撤回配额上限兜底防误撤。
4. **LLM 撤回工具**：注册 `recall_last_message` 工具，LLM 在对话中发现自己的回复不合适时可主动调用撤回。
5. **手动撤回命令（管理员专用）**：`/recall` 或 `/撤回` 撤回 bot 最近一条消息（2 分钟时限内）。触发者须在 `recall.admin_ids` 管理员列表中，本地控制台（bot_console）不受限制。
6. **分段感知（v1.2.0 新增）**：与智能分段插件（如 [smart_segmentation_plugin](https://github.com/saberlights/smart_segmentation_plugin)）共存时，一条回复被拆成多条连续消息发送——聚合窗口（`recall.group_window_seconds`，默认 6 秒）内的出站消息合并为一条逻辑回复：
   - **自评按合并后完整文本判定一次**（N 段只烧 1 次 LLM 调用，不再逐段评审）；
   - **`/recall` 与 LLM 撤回工具一次撤回该回复的全部段**（共享一次撤回配额）；
   - 组内超 2 分钟（QQ 撤回时限）的段自动剔除，剩余段照常可撤；
   - 复读跟读不受影响：分段插件只处理主回复链路的产出，不会切分插件自己发送的消息。

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
| review_task | `""` | 自评用的**模型任务名**（`utils` / `replyer` / `planner` / `vlm` …），走 SDK 的 `task_name` 参数；留空则由 SDK 默认任务决定（MaiBot 1.2.5 起默认 `utils`）。**这是「任务」不是「模型」** |
| review_model | `""` | 自评用的**具体模型名**（`model_config.toml` 里 `[[models]]` 的 `name`），走 `model` 参数；留空则用该任务的模型选择策略。⚠️ MaiBot 1.2.5 起「任务名」与「模型名」是两个独立参数，把任务名（如 `planner`）填在这里会报「未找到名为 planner 的模型」——任务名请填 `review_task` |
| review_timeout_seconds | 60 | 单次自评 LLM 调用超时秒数（5–300）。插件侧超时只是放弃等待，Host 仍会把请求跑完并计费；自评模型较慢（平均耗时 >20s）时建议调大，避免反复超时触发熔断 |
| context_messages | 10 | 自评时带入的近期聊天记录条数，用于语境判断（接梗/玩谐音/回应点名不算跑题）；0 = 关闭语境感知，仅凭消息本身判断。历史接口异常时自动降级为纯文本判定，不影响撤回链路（0–50） |
| max_recalls_per_hour | 6 | 每小时撤回总次数上限（含自评/工具/手动命令）（1–60） |
| admin_ids | [] | 可用 `/recall`/`/撤回` 命令的管理员 QQ 列表，兼容 `["qq:123456789"]` 格式；为空时仅本地控制台（bot_console）可用。LLM 工具与自评撤回不受此列表限制 |
| group_window_seconds | 6.0 | 分段聚合窗口（0.5–30 秒）：与智能分段插件共存时，间隔小于该窗口的出站消息合并为一条逻辑回复（整组撤回 + 聚合自评一次）。需大于分段插件的补发间隔（宿主 `response_post_process.typing_speed` 越大间隔越长）；不用分段插件时保持默认即可 |

## 命令

| 命令 | 说明 |
|---|---|
| `/recall` 或 `/撤回` | **管理员专用**：手动撤回 bot 最近一条消息。触发者须在 `recall.admin_ids` 中，或来自本地控制台 |

## 安全边界

- `/recall` 与 `/撤回` 仅 `recall.admin_ids` 内成员或本地控制台（bot_console）可用；
  `admin_ids` 为空时远程一律拒绝。LLM 工具与自评撤回不走该鉴权（设计如此）。
- 自评提示词用 `<<<MSG>>>` / `<<<END>>>` 分隔待审文本，注入前会把文本里的连续尖括号中和，
  防止群友用伪造分隔符闭合段落、注入指令。
- `max_recalls_per_hour` 对自评 / LLM 工具 / 命令三条撤回路径统一生效。
- 自评 LLM 调用超时可配置（`review_timeout_seconds`，默认 60 秒），失败/超时连续 3 次熔断 10 分钟，避免刷屏与后台任务堆积。
- 纯占位文本出站消息（如 `[voiceurl消息]`、`[图片]`）没有可判定语义，跳过自评省一次 LLM 调用；消息仍记录，撤回不受影响。
- 自评失败熔断时，若错误是「未找到名为 xxx 的模型」，日志会附带**本次实际传出的 `model` 参数值**与
  **Host 当前可用模型任务名清单**（经 `llm.get_available_models`），不需要再去翻 `model_config.toml` 猜。

## 测试方式（本地，无需真机）

```bash
# 在 MaiBot插件开发/ 目录下
.venv/Scripts/python.exe check_plugin.py plugins/repeater-recall          # 结构自检
.venv/Scripts/python.exe smoke_test.py plugins/repeater-recall            # 生命周期冒烟
.venv/Scripts/python.exe test_repeater_recall.py                          # 功能直调测试（100 项）
.venv/Scripts/python.exe test_repeater_recall_extra.py                    # 上线前增量 QA（47 项）
.venv/Scripts/python.exe test_repeater_recall_seg.py                      # 分段感知专项（21 项，v1.2.0）
.venv/Scripts/python.exe run_gates.py plugins/repeater-recall             # check + smoke 双门禁
```

基础套件覆盖：装饰器-函数配对、复读触发/冷却/分流、过滤规则、撤回四态（成功/失败/适配器缺失/超时）、
配额、自评三态（该撤/不撤/垃圾响应）、命令鉴权五种身份、**after_send 记录五态**（主链路含负数
message_id / sent=False / 缺 ID / 非 bot / 登录信息不可用降级）、**历史兜底六态**（命中/无 bot 消息/
过期/缺时间戳/内存优先/接口故障静默）、**manifest 能力声明 + 退避熔断九态**（失败退避/退避期内不重试/
退避过期恢复/成功后缓存/自评熔断/熔断不误伤撤回/review_model 透传/默认空串/成功后清零）。

增量 QA 覆盖：on_unload 在途任务回收、惰性清理四态（统计/冷却/消息记录/上下文缓存）、
**提示注入分隔符中和**、配额滑动窗口过期、**通用入口软失败降级强类型入口**、
**自评 LLM 超时保护**（Host RPC 无内建超时，卡住会让自评任务永久挂起）、
平台消息 ID 两种键形态、自评撤回不误删更新的记录、静态扫描（无硬编码绝对路径/QQ 号/密钥）、
**「未找到名为 xxx 的模型」熔断日志自诊断**（带出本次 model 参数值 + Host 可用模型任务名清单）。

## 常见问题

- **撤回报"没有可撤回的 bot 消息"（v1.0.2 已修复主因）**：v1.0.1 只记录插件自己发出的消息，Maisaka 主链路的日常回复拿不到 `platform_message_id`。v1.0.2 增加 `send_service.after_send` 全量记录 + 历史回溯兜底。若仍报该提示，请查日志：
  - 出现 `[诊断] after_send 未取到平台消息 ID：stream=... keys=[...]` → 说明该 Host 版本的 `after_send` 载荷回填时机晚于钩子，把 `keys=` 里的字段列表贴出来；此时可先依赖历史回溯兜底。
  - 出现 `get_login_info 失败，历史兜底定位 bot 消息将不可用` → napcat-adapter 未连接，历史兜底失效，只剩钩子记录一条路。
- **撤回报"napcat 适配器不可用（RPCError / RPCError）"**（v1.0.3 已修）：日志里会同时出现
  `未获授权能力: api.call`。官方手册把 `api.call` 列为「免声明」，**实测 Host 1.2.3 仍会拒绝**——
  必须在 manifest 的 `capabilities` 里显式写上 `"api.call"`，改完**完整重启** MaiBot（热重载不生效）。
  v1.0.3 已补上，升级后若仍报此错，说明插件目录没换干净或没重启。
- **自评报「未找到名为 'planner' 的模型」/「未找到名为 'utils' 的模型」（MaiBot 1.2.5 前后最常见，v1.2.2 起日志自带答案）**：
  **根因是「模型任务名」与「具体模型名」被混填，或者模型列表本身是空的。**

  MaiBot 1.2.5 的 changelog 写得很明确：
  > 插件 SDK/API：修复插件 LLM 能力把具体模型名误当作任务名解析的问题，支持分别指定模型任务与具体模型。

  也就是说 1.2.5 起这两个参数**分开了**：

  | 参数 | 含义 | 落哪个配置 |
  |---|---|---|
  | `task_name` | **模型任务名**（`utils` / `replyer` / `planner` / `vlm` …） | `recall.review_task` |
  | `model` | **具体模型名**（`model_config.toml` 里 `[[models]]` 的 `name`） | `recall.review_model` |

  两个典型症状与对应处置：

  | 日志里出现的名字 | 说明 | 处置 |
  |---|---|---|
  | 只有 `'planner'` 之类**特定**任务名 | 把任务名填进了 `review_model`（那里只认具体模型名） | 把它挪到 `review_task`，或清空 `review_model` |
  | **`'utils'` 也报找不到** | 模型层整体解析不到——`utils` 是 SDK 1.2.5 起的**默认任务名**，不是谁填的。几乎可以断定 **WebUI 里模型列表为空**（1.2.5 的 WebUI 1.7.4 正是在修「模型列表为空时无法添加提供商」这个坑） | 去 WebUI：**先保存提供商（provider），再添加模型**，最后把任务指到具体模型 |

  辅助手段：熔断日志里会打出 `本次调用 review_task=... review_model=...` 与
  `Host 当前可用模型任务名：...`（经 `llm.get_available_models`）；后者为空时插件会直接提示
  「模型列表为空」。临时止血可把 `recall.self_review` 设为 `false`——撤回工具与 `/recall` 命令不受影响。
- **自评每天狂刷 `qwen3.7-text-embedding ... Field required: input.contents`**：Host 没给本插件的 LLM
  任务（`plugin.org.mai-mai.repeater-recall`）配文本生成模型，fallback 到了向量模型。三选一：
  1. 在 `model_config.toml` 里给任务 `plugin.org.mai-mai.repeater-recall` 配一个文本模型（根治）；
  2. 把插件配置 `recall.review_model` 填成具体模型名（绕过任务路由，改完热重载即可）；
  3. 把 `recall.self_review` 设为 `false` 彻底关闭自评。
  自评连续失败 3 次会自动熔断 10 分钟并停止刷屏，**撤回工具与 `/recall` 命令不受自评影响**。
  v1.0.9 起自评 LLM 调用带超时保护（现由 `review_timeout_seconds` 配置，默认 60 秒），超时按失败计入熔断，
  不会让自评任务永久挂在后台——只有插件卸载才能回收的旧行为已修掉。
- **自评 LLM 调用量没有上限**：bot 每条逻辑回复触发一次自评（v1.2.0 起分段回复聚合为一条逻辑回复，
  N 段只调 1 次 LLM；v1.2.0 之前逐段各调一次）。即使一次都不撤回也照样消耗 token。
  `max_recalls_per_hour` 只限制「撤回次数」，不限制「自评次数」。
  高频群建议调大 `review_delay_seconds` 或把 `context_messages` 设为 0 缩短 prompt。
- **一次回复被切成多段，撤回只撤掉最后一段**（v1.2.0 已修）：分段插件把一条回复拆成多条独立消息、
  各有自己的 `message_id`。v1.2.0 起插件按 `group_window_seconds` 聚合分段，`/recall` 与撤回工具
  一次撤回该回复的全部段。若仍只撤一段，检查聚合窗口是否小于分段补发间隔（调大
  `group_window_seconds`，或调小宿主 `response_post_process.typing_speed`）。
- **与智能分段插件（smart_segmentation 等）共存的推荐配置**：
  - 宿主 `bot_config.toml` 关闭内置分段：`[response_splitter] enable = false`（分段插件 README 要求）；
  - 分段插件用小模型（`gpt-4o-mini` / `qwen-plus` 一类）即可，与本插件的自评模型互不影响；
  - 若分段后自评出现「一条回复评了多次」，说明分段间隔超过了 `group_window_seconds`，调大该值
    （上限 30 秒）即可；反之若两条独立回复被误并成一组一起撤回，调小该值。
- **撤回报"适配器不可用"**：检查 napcat-adapter 是否加载、NapCat 是否在线；`ctx.api.call` 报「API 不存在」通常是适配器未装或版本 < 1.4.0。
- **撤回报超时限**：QQ 平台限制约 2 分钟，插件按 120 秒保守截断。
- **复读不触发**：确认文本长度 ≥ min_length、不是纯数字/超短英文单词、不在冷却期；`/命令` 消息不参与统计。
- **自评从不撤回**：LLM 判定偏保守属正常设计（仅明显不合适才撤）；可调低 `review_delay_seconds` 观察 WebUI/日志中自评记录。
- **命令提示"仅管理员可用"**：把触发者的 QQ 号加进 `recall.admin_ids`（WebUI 插件配置或 config.toml 均可，热重载生效），格式 `["123456789"]` 或 `["qq:123456789"]`；本地控制台始终可用。

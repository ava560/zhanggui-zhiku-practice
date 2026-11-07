# 掌柜智库实践版

面向普通用户的金融知识库。用户上传产品资料、公告、政策和常见问题，再用自然语言提问。系统从已入库资料里检索，并给出可追溯的回答。

它是资料查询和解释工具：不提供个性化投资建议，不承诺收益，不替代正式产品文件、监管文件或人工判断。业务规则见 [docs/requirements.md](docs/requirements.md)，实现方案见 [docs/design.md](docs/design.md)。

## 能做什么

- **资料入库**：上传 PDF、Markdown 或 Word，解析后写入对象存储、向量库和知识图谱。
- **对话查询**：在已有对话里提问。浏览器保存匿名用户 Cookie，服务端为每段对话生成 `conversation_id`。
- **带来源的回答**：资料回答附来源；网页来源附链接。
- **名称确认**：主体名称相似度大于等于 0.9 直接采用；0.6 至低于 0.9 交给用户确认，可以多选。
- **长期记忆**：只保存用户明确要求记住，或在个性化设置里写入的内容。

页面：

| 地址 | 用途 |
|---|---|
| http://127.0.0.1:8000 | 上传入库 |
| http://127.0.0.1:8000/chat | 对话 |

## 问答怎么走

一次提问先由模型分成三类，再进入对应路径。入口在 `search/agent_workflow.py`。简单查询不经过主 Agent。

```mermaid
flowchart TD
    U[问题、最近十条消息、会话记忆、长期记忆] --> L[意图识别]
    L --> C[闲聊：直接回答]
    L --> S[简单查询]
    L --> R[复杂任务：主 Agent]
    S --> Q[共用私有库查询]
    R --> P[私有库子 Agent]
    R --> W[联网子 Agent]
    P --> Q
    Q --> A[带来源的回答]
    W --> A
    R --> A
```

| 路径 | 代码 | 行为 |
|---|---|---|
| 闲聊 | `search/intent_classify.py` | 不查资料、不联网，直接回答 |
| 简单查询 | `search/shared_query.py` | 走一次共用私有库查询，然后组织回答 |
| 复杂任务 | `search/react_agent.py` | 主 Agent 编排两个子 Agent。补查最多两轮，派发少于 5 次 |

意图识别必须调用 LLM。模型只输出 `chat`、`simple`、`complex` 之一。输出无法解析时再调用一次；第二次仍无法解析则停止，并提示分类失败。

## 各 Agent 的内部结构

三个角色的分工：意图识别只分流；主 Agent 只编排；两个子 Agent 只取证。最终回答由 `search/agent_workflow.py` 在简单查询结束或主 Agent 结束时生成。

### 主 Agent

实现在 `search/react_agent.py`，规划提示在 `search/agent_workflow.py` 的 `_plan_with_llm`。它是一个有界 ReAct 循环：每轮先看已有证据，再决定动作，再读取该动作的结果。

```mermaid
flowchart TD
    start[目标、已派发次数、已补查轮次、证据包] --> plan[LLM 规划本轮动作]
    plan --> kind{动作}
    kind -->|finish 或没有动作| done[汇总证据并结束]
    kind -->|ask_user| pause[暂停，把名称确认交给用户]
    kind -->|private_kb| priv[调用私有库子 Agent]
    kind -->|web_search| web[调用联网子 Agent]
    priv --> check{证据是否都已支持且没有缺口}
    web --> check
    check -->|是| done
    check -->|否，且补查未满两轮| plan
    check -->|已满两轮或派发已满| done
```

允许的动作只有四种：

| 动作 | 作用 |
|---|---|
| `private_kb` | 把一个子问题交给私有库子 Agent |
| `web_search` | 把一个子问题交给联网子 Agent |
| `ask_user` | 暂停，等待用户确认名称 |
| `finish` | 停止派发，把已收集的证据交给回答步骤 |

内部状态只有四项：用户目标、已派发次数、已完成的补查轮次、各子任务交回的证据包。初始拆分不算补查。一轮里可以同时给出多个互不依赖的派发；私有库和联网合计达到 4 次后不再派发。证据全部为 `supported` 且没有缺口时提前结束。达到上限时仍用已有证据回答，并写明未完成的缺口。子 Agent 不能再创建子 Agent，也不能直接向用户发布最终回答。

### 私有库子 Agent

实现在 `search/self_ask.py` 的 `run_private_self_ask`。它用 Self-Ask 取证：先查主问题，再决定要不要追问。

```mermaid
flowchart TD
    q[子任务里的一个问题] --> once[共用私有库查询]
    once --> stop{等待确认或检索失败?}
    stop -->|是| back[交回证据包并暂停或报错]
    stop -->|否| ask{LLM 是否给出追问}
    ask -->|不需要，或已有两个追问| back2[交回证据包]
    ask -->|给出追问| again[同一套共用查询再查一次]
    again --> ask
```

每个追问都再执行一次共用私有库查询，追问结果并入同一证据包。追问最多两个。证据已经支持且没有缺口时不再追问。名称待确认或检索失败时立即返回，不再追问。返回内容是证据包：改写后的问题、已确认名称、资料块、追问列表、状态和缺口。它不调用回答模型。

### 联网子 Agent

实现在 `search/self_ask.py` 的 `run_web_self_ask`。结构与私有库子 Agent 相同，检索端口只有百炼 MCP。

1. 用子任务中的问题调用一次网页检索。
2. 丢掉没有链接的摘要。
3. 由同一个追问判断决定是否再查，最多两个追问。
4. 把标题、链接和摘要收进证据包交回主 Agent。

用户关闭联网搜索时，这一支直接返回缺口「未开启联网搜索」，不调用百炼。网页检索失败时写入联网不可用，已取到的带链接结果仍然保留。

## 简单查询流程

简单查询在意图被标成 `simple` 后，调用一次 `run_shared_query`，再由 `_finish_simple` 组织回答。私有库子 Agent 的主问题和每个追问走同一函数，停在证据包，不进入下面的回答步骤。

```mermaid
flowchart TD
    start[当前问题、最近十条、会话记忆、长期记忆] --> rewrite[LLM 改写问题并提取名称]
    rewrite --> hasName{提取到名称?}
    hasName -->|否| direct[按改写后的问题检索资料块]
    hasName -->|是| vec[BGE-M3 稠密加稀疏匹配名称]
    vec --> score{最高分}
    score -->|大于等于 0.9| adopt[直接采用，可多个]
    score -->|0.6 至低于 0.9| ask[暂停，交给用户确认]
    score -->|低于 0.6 或没有命中| reg[注册表最长匹配]
    reg --> amb{简称仍指向多个主体?}
    amb -->|是| ask
    amb -->|否| named[只检索同名资料块]
    adopt --> named
    named --> hyde[HyDE 生成假设文本，再检索一路]
    direct --> hyde
    hyde --> web{用户打开了联网搜索?}
    web -->|是| mcp[百炼取网页标题、链接和摘要]
    web -->|否| rrf[RRF 融合]
    mcp --> mcpOk{联网成功?}
    mcpOk -->|是| rrf3[普通检索、HyDE、网页摘要三路 RRF]
    mcpOk -->|否| rrf2[保留前两路，并记录联网不可用]
    rrf --> answer[BGE 重排后生成回答并附来源]
    rrf3 --> answer
    rrf2 --> answer
```

步骤对应 `search/shared_query.py` 和 `build_ports`：

1. **改写。** 最近十条消息、会话记忆和长期记忆一起交给模型。长期记忆只作为用户偏好和已知事实。模型返回改写后的问题和提取出的名称，可以是多个。
2. **无名称。** 跳过名称索引，用改写后的问题直接混合检索资料块。概念、公告、政策和业务规则走这一支。
3. **有名称。** 每个名称用 BGE-M3 做稠密、稀疏双向量检索，取得分最高的一条。大于等于 0.9 直接采用；0.6 至低于 0.9 暂停，页面请用户确认或复选。确认之前不检索正文。
4. **注册表。** 向量没有可直接采用或待确认的名称时，按规范名做最长匹配。短名不能截走已经被更长名称占用的文字。剩余简称仍对应多个主体时，同样暂停请用户选择。最终没有任何可查名称时，回答说明未检索到足够信息。
5. **两路正文检索。** 有名称时只查同名资料块；无名称时不过滤名称。HyDE 根据问题生成一段假设性检索文本，和原问题拼在一起再查一次。假设文本只用于检索，不写入回答。
6. **联网。** `use_web_search` 打开时调用百炼，默认打开。失败时保留前两路结果，并在回答里说明联网不可用。没有链接的网页摘要不参与排序。
7. **RRF。** 每一路先按自己的顺序去重。同一条结果在同一路只贡献一次 `1/(60+名次)`，多路分数相加后从高到低排列。
8. **回答。** 融合结果再用 BGE-reranker-large 重排，然后由回答模型生成文字。回答附上文件名或网页链接；库内文件能对上时给出下载地址。没有资料块时不调用回答模型，直接说明未检索到足够信息。检索连接失败时返回检索错误，不把故障写成库里没有资料。

用户完成名称确认后，下一条请求带着已选名称继续同一次查询，不再重新做 0.6 至 0.9 的分档。

## 入库怎么走

网页上传和命令行导入共用 `upload/main_graph.py` 里的 LangGraph：

`校验文件 → 原件存入 MinIO → 解析文档 → 处理图片 → 文档切片 → 识别主体 → BGE-M3 向量化 → 写入 Milvus → 写入知识图谱`

单文件上限默认 200 MB。后台按单任务队列顺序处理，页面可以一次选择多份文件。

命令行导入时，把文件放到 `input/`，或直接传入路径：

```powershell
D:\python\python.exe -m upload.main_graph
D:\python\python.exe -m upload.main_graph "D:\路径\某份资料.pdf"
```

文件名被判定为与金融无关时会跳过。

## 目录

| 路径 | 内容 |
|---|---|
| `api.py` | 网站入口：上传、对话、记忆、资料下载 |
| `web/` | 上传页和聊天页 |
| `upload/` | 入库流水线 |
| `search/` | 意图识别、共用查询、主 Agent、两个子 Agent、回答与引用 |
| `knowledge/` | 主体注册表和知识图谱抽取 |
| `research/` | 子任务结构、预算和并行分发 |
| `chat_service.py` | 对话、待确认暂停、MongoDB 消息 |
| `tests/` | 单元测试和流程测试 |
| `docs/` | 需求与设计 |
| `input/` | 命令行导入时扫描的资料 |
| `output/` | 检索和导入产生的中间结果 |

## 启动

先在项目根目录准备 `.env`，填入 MinerU、模型、MinIO、Milvus、MongoDB、Neo4j、LLM 和百炼 MCP 的现有地址。然后在本目录执行：

```powershell
.\start_web.ps1
```

脚本固定使用 `D:\python\python.exe`，并启动 `uvicorn api:app`。

常用环境变量：

| 变量 | 作用 | 默认 |
|---|---|---|
| `WEB_HOST` | 监听地址；局域网可设 `0.0.0.0` | `127.0.0.1` |
| `WEB_PORT` | 端口 | `8000` |
| `WEB_MAX_UPLOAD_MB` | 单文件上限 | `200` |
| `MONGO_CHATS_COLLECTION` | 对话列表集合 | `conversations` |
| `MONGO_CONVERSATION_COLLECTION` | 消息集合；也认 `MONGODB_CONVERSATION_COLLECTION` | `conversation_history` |
| `BGE_RERANKER_PATH` 或 `BGE_RERANKER_LARGE` | 重排模型目录 | 无，需在 `.env` 配置 |
| `SEARCH_USE_PROXY` | 设为 `1` 时，问答链使用当前 HTTP 代理 | 已配置的服务地址直连 |
| `MILVUS_USE_PROXY` | 设为 `1` 时，Milvus 走代理 | 直连 |

未传 `web_search` 时，对话接口默认打开联网搜索。请求里的 `mode` 可以是 `auto`、`fast` 或 `deep`。

匿名 Cookie 适合本机或受控内网演示。对外提供多用户服务时，应接入正式登录。

## 测试

在项目根目录运行，例如：

```powershell
D:\python\python.exe -m pytest tests/test_agent_flow.py tests/test_api_entry.py
```

会访问真实 MongoDB、Milvus、模型和百炼的冒烟脚本：

```powershell
D:\python\python.exe -m tests.live_chat_smoke
D:\python\python.exe -m tests.live_web_chat_smoke
```

第二个命令需要网页服务已经启动，并且测试使用的端口与 `WEB_PORT` 一致。

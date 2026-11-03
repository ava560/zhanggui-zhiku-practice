# 灵库：产品资料入库与小灵问答

网站把现有文档处理节点串成一条可视化入库流水线：

`上传文件 → MinIO 原件存储 → PDF/Markdown 解析 → 图片处理 → 文档切片 → 商品识别 → BGE-M3 向量化 → Milvus 入库`

## 启动

先确保项目根目录 `.env` 已配置现有的 MinerU、模型、MinIO 和 Milvus 参数，然后在本目录执行：

```powershell
.\start_web.ps1
```

启动脚本固定使用已经具备 MinIO、Milvus、LangGraph 和 BGE 依赖的
`D:\python\python.exe`，避免误用 `python-learning\.venv` 后出现依赖不一致。

浏览器访问：<http://127.0.0.1:8000>

聊天页：<http://127.0.0.1:8000/chat>。左侧可新建和切换对话；每段对话由服务端生成独立的 `conversation_id`，浏览器通过持久 Cookie 持有匿名 `user_id`。接口始终同时使用这两个编号查询，问题和模型回答分别写入 MongoDB 的单条消息记录。

聊天使用项目 `.env` 中显式配置的 MongoDB、Milvus、BGE-M3、BGE-reranker-large、LLM 和百炼 MCP 地址。默认将对话列表存入 `conversations` 集合，将消息存入 `conversation_history` 集合；可用 `MONGO_CHATS_COLLECTION` 和 `MONGO_CONVERSATION_COLLECTION` 覆盖集合名。普通产品问题执行：`MongoDB 历史 → 商品提取/问题改写 → Milvus 商品确认 → 普通检索 + HyDE 检索 → RRF → MCP 网页搜索 → BGE 重排 → 最终回答与 MongoDB 留档`。询问 API 密钥、内部配置或检索块等内部数据时，小灵会直接拒绝并留档，不调用检索和模型。商品名处于 0.6–0.9 匹配区间时，页面提供确认或多选；无匹配商品时返回“暂无查询结果”。联网搜索临时失败时会明确提示，并仅用已检索的知识库资料继续回答。上传环节将图片存入 MinIO，回答中被选中的图片下载地址可直接点击。匿名 Cookie 适合本机或受控内网演示；如对外提供多用户服务，应接入正式登录认证。

重排模型目录由 `BGE_RERANKER_PATH` 或 `BGE_RERANKER_LARGE` 指定。本机 `.env` 已指向 `D:\codex\models\BAAI\bge-reranker-large`。问答链默认让已配置的 LLM/MCP 地址直连，避免本机失效的 HTTP 代理；如确需代理，设置 `SEARCH_USE_PROXY=1`。

支持通过环境变量调整：

- `WEB_HOST`：监听地址，默认 `127.0.0.1`；局域网访问可设为 `0.0.0.0`。
- `WEB_PORT`：端口，默认 `8000`。
- `WEB_MAX_UPLOAD_MB`：单文件大小上限，默认 `200`。
- `MILVUS_USE_PROXY`：默认直连 Milvus，避免继承的 HTTP 代理干扰 gRPC；如果所在网络必须通过代理访问 Milvus，设为 `1`。

为保护当前 BGE 模型单例及旧节点共用的 `output/chunks.json`，后台按单任务队列顺序处理；网页可以一次选择多份文件。

真实服务冒烟测试（会创建新的 MongoDB 测试会话，并实际调用 LLM、Milvus、MinIO、MCP）：

```powershell
D:\python\python -m tests.live_chat_smoke
D:\python\python -m tests.live_web_chat_smoke
```

第二个命令需先启动网页服务；若 8000 端口已被占用，可先设置 `WEB_PORT`，测试命令使用相同端口。

# pterodactyl-rag

一个独立的 **插件文档** 检索索引,面向 Pterodactyl / Minecraft 场景,通过 **MCP 协议**
以只读工具的形式提供给 DeerFlow agent。

它让面板 agent(`deerflow.community.pterodactyl`)在需要了解某个插件如何配置时
——权限节点、配置项、命令——可以直接查文档,而不是凭记忆猜。入库(切片 → 向量化 →
写入)是一个离线 CLI 步骤;检索则暴露为 MCP 工具。

## 为什么是一个独立的包

它 **刻意** 放在 `deerflow-harness` 的 uv workspace 之外:它依赖一批较重的可选
依赖(`psycopg`、`pgvector`、`pypdf`、`beautifulsoup4`),这些不应进入 Gateway 进程。
DeerFlow 把它作为一个 **独立的 MCP server 进程** 启动(默认 stdio),从而让这些依赖
保持隔离。这些重依赖在代码里是 **惰性导入** 的,所以纯逻辑模块在任何环境都能加载。

## 架构总览

```
docs 目录 ──入库──▶ loaders ─▶ splitter ─▶ tags ─▶ embeddings ─▶ pgvector store
                                                                       │
 agent ◀── MCP 工具 ◀── server ◀── retriever ◀── 余弦相似度 + 标签过滤 ─┘
```

- **loaders**(加载)— md / txt / html / pdf,支持 YAML frontmatter 和 `.rag.yaml` 同级配置。
- **splitter**(切片)— 按 token / 标题感知切块(tiktoken,携带 `heading_path` 面包屑)。
- **tags**(标签)— 命名空间化标签(`plugin:essentialsx`、`category:config`);从路径推断,
  可被 frontmatter 覆盖。
- **embeddings**(向量化)— 批量调用 OpenAI 兼容的 `/embeddings` 接口,带重试。
- **store**(存储)— `pgvector`(Postgres,独立的 `pterodactyl_rag` schema,HNSW + GIN
  索引);测试用内存存储。检索时先按标签过滤再按余弦排序,一次扫描完成,并带
  **软回退**(命中为空的过滤条件会被丢弃,结果标记 `relaxed`)。
- **retriever**(检索)— 向量化查询,对 `plugin` 提示做模糊归一化,施加软标签过滤,截断片段。
- **server**(服务)— FastMCP server,暴露下文的 5 个工具。

---

## RAG 系统是如何工作的(从切片开始)

整条链路分为 **入库(离线,CLI)** 与 **检索(在线,MCP 工具)** 两半。下面按数据实际
流经的顺序逐段说明。

### 1. 切片(splitter)

切片的目标是把一篇文档拆成大小合适、且 **可被引用** 的片段(chunk)。

- **Markdown**:按 ATX 标题(`#`~`######`)切分为若干「小节」。切片器维护一个按层级的
  标题栈,为每个小节生成 `heading_path` 面包屑(例如 `Config > Permissions`),供检索结果
  引用定位。首个标题之前的正文归为 `heading_path=None` 的小节。
- **非 Markdown(txt/html/pdf)**:整体作为一个无标题小节处理(按段落)。
- **token 预算**:每个小节再按 token 数切成窗口,窗口上限为 `PTERO_RAG_CHUNK_TOKENS`
  (默认 800),窗口之间有 `PTERO_RAG_CHUNK_OVERLAP`(默认 120)的重叠,以免语义在边界被切断。
  token 计数优先用 `tiktoken`(`cl100k_base`);若环境缺少 tiktoken,则退化为空白分词计数,
  保证切片器在任何环境可用。
- 产出的每个 `Chunk` 携带:`chunk_index`(顺序)、`content`、`heading_path`、`token_count`,
  以及从文档继承来的 `tags`。

### 2. 标签(tags)

标签是 `namespace:value` 形式(如 `plugin:essentialsx`、`category:config`),在库里以
Postgres `TEXT[]` 存储,并用一个 GIN 索引覆盖。它承担两个职责:

- **推断(resolve)**——从文档 **路径** 零配置推断标签:路径第一段是插件名
  (`EssentialsX/config/kits.md` → `plugin:essentialsx`),第二段若命中已知类别
  (`config`/`permissions`/`commands`/`faq`/`api`/`install`)则为 `category:`。
  frontmatter / `.rag.yaml` 可追加 `tags:`,或用 `plugin:` **替换** 路径推断出的插件名
  (用于纠正命名不规范的目录)。
- **归一化(normalize)**——把任意标签统一化(小写、去空白、连字符化、去重、限长),
  使 `EssentialsX` / `essentials x` / `Essentials-X` 都收敛到 `plugin:essentialsx`。
  这正是检索侧能够「宽容匹配」的基础。上限:每篇文档 32 个标签、每个标签 80 字符。

> agent 在入库阶段 **完全不参与**,标签由 pipeline 自动生成。

### 3. 向量化(embeddings)

- `OpenAIEmbedder` 是对 OpenAI 兼容 `/embeddings` 接口的轻量异步封装(仅依赖 `httpx`),
  可对接 OpenAI、Azure、本地 vLLM 等任意兼容服务端。
- **批量**发送(默认 64 条/批),对 429 / 5xx 做 **指数退避重试**(默认 5 次,上限 30s),
  并按响应的 `index` 字段 **保序**,确保向量与切片一一对应。
- 向量 **维度**(`PTERO_RAG_EMBED_DIM`,默认 1536)在首次入库时被写入索引元数据并 **锁定**。

### 4. 写入 / 入库(store + pipeline)

`pterodactyl-rag ingest` 遍历 `PTERO_RAG_DOCS_DIR` 下每个受支持文件,对每篇文档:

1. 计算内容 SHA-256。若库中已有同 `source_path` 且哈希未变 → **跳过**(增量入库)。
2. 切片 → 向量化 → `upsert_document`(先删该文档旧 chunks 再插新的)。
3. 标签被 **反规范化** 到每个 chunk 上,使得检索时「标签过滤 + 向量排序」能在
   **单次扫描** 内完成(`WHERE c.tags @> %s ORDER BY c.embedding <=> %s`)。
4. 全部处理完后,默认 **prune**:删除源文件已消失的已索引文档(`--no-prune` 可关闭)。

维度/模型一致性:`store.initialize` 在打开已有索引时,如果存储的维度或模型与当前配置
不一致,会 **拒绝启动并抛出 ValueError**——避免用不兼容的向量去查询。要更换模型/维度,
须先 `reset` 再重新入库。

### 5. 检索(retriever)

一次 `rag_search` 的处理流程:

1. **构造过滤标签**:把 agent 传入的松散提示转成规范标签。`plugin` 会做
   **模糊归一化**——先归一化取值,若不在已知 facet 中,则用 difflib 对已知插件名做
   相似度匹配(cutoff 0.8);仍无匹配就保留归一化后的猜测,交给存储侧软回退兜底。
   `category` / `platform` / `lang` 及原始 `tags` 也各自归一化。
2. **向量化查询**并在存储中检索(`top_k` 默认 5)。
3. **软过滤**:由存储层负责——若过滤条件命中为空,则 **丢弃过滤条件、退回无过滤检索**,
   并把结果标记 `relaxed=true`,附带一条 `note` 说明(绝不返回硬性空结果)。
4. **截断片段**:过长的 chunk 文本截断到 500 字符,避免灌满模型上下文。

返回结构固定为 `{"hits": [...], "filter": {"applied", "relaxed", "note"}}`,每个 hit
带 `plugin` / `title` / `heading_path` / `source_path` / `chunk_index` / `score` /
`snippet` / `tags`——便于 agent 直接引用来源。

### 6. 通过 MCP 提供给 agent(server)

`server.py` 用 FastMCP 把上述能力注册为 5 个 **只读** 工具(见下)。工具实现放在可测试的
`RagService` 上(接收一个 store 和一个可选 retriever),因此测试可以直接对着内存存储 +
假向量器驱动,无需真实 DB 或 MCP 传输。每个工具都 **自行捕获异常**,失败时返回一条
可读的 `note` 字符串而不是抛出——单次检索故障降级为一条模型能理解并重试的提示,
而不会中断整个 run。若未配置嵌入 API key,`rag_search` 返回不可用提示,但
发现类 / 统计类工具仍可用。

---

## 配置

全部配置通过 `PTERO_RAG_*` 环境变量提供。**密钥只允许通过 `$VAR` 引用**
(来自 `extensions_config.json`),绝不内联、绝不打日志。

| 变量                         | 必填       | 默认值                      | 用途                                           |
|----------------------------|----------|--------------------------|----------------------------------------------|
| `PTERO_RAG_DATABASE_URL`   | 是        | —                        | Postgres DSN(可复用 `$DATABASE_URL`)            |
| `PTERO_RAG_EMBED_API_KEY`  | 入库/检索时必填 | —                        | 嵌入 API key(`$OPENAI_API_KEY`);对接本地服务填占位串即可   |
| `PTERO_RAG_EMBED_BASE_URL` | 否        | OpenAI                   | OpenAI 兼容端点(指向 Ollama / HF TEI 等本地服务即可用本地模型) |
| `PTERO_RAG_EMBED_MODEL`    | 否        | `text-embedding-3-small` | 嵌入模型                                         |
| `PTERO_RAG_EMBED_DIM`      | 否        | `1536`                   | 嵌入维度(入库时锁定)                                  |
| `PTERO_RAG_DOCS_DIR`       | 入库时必填    | —                        | 要遍历的插件文档目录                                   |
| `PTERO_RAG_CHUNK_TOKENS`   | 否        | `800`                    | 每个 chunk 的最大 token 数                         |
| `PTERO_RAG_CHUNK_OVERLAP`  | 否        | `120`                    | chunk 重叠 token 数(须小于 chunk tokens)           |
| `PTERO_RAG_TOP_K`          | 否        | `5`                      | 默认返回结果数                                      |
| `PTERO_RAG_TRANSPORT`      | 否        | `stdio`                  | `stdio` 或 `http`                             |

嵌入维度在首次入库时记录到索引上;若索引中存储的维度/模型与当前配置不同,server
**拒绝启动**——需 reset 后重新入库才能更改。

> 想用 **本地模型**(Ollama / HuggingFace TEI)完全免 OpenAI key?见下文
> 「先入库一次 → 用本地模型跑向量化」小节:把 `PTERO_RAG_EMBED_BASE_URL` 指向本地端点、
> `PTERO_RAG_EMBED_DIM` 改成模型真实维度即可。

## 命令行(CLI)

```bash
pterodactyl-rag ingest        # 遍历 PTERO_RAG_DOCS_DIR,切片/向量化/写入,并清理已删除文档
pterodactyl-rag ingest --no-prune
pterodactyl-rag serve         # 启动 MCP server(stdio,或按配置走 http)
pterodactyl-rag stats         # 打印索引健康状况
pterodactyl-rag reset --yes   # 删除索引 schema(破坏性操作)
```

### 用项目根 Makefile 一键操作(推荐)

仓库根的 `Makefile` 封装了 RAG 扩展栈(pgvector + rag-mcp)的生命周期,以及本地调试用的向量库操作,省去手敲路径:

```bash
# —— RAG 扩展栈(容器化 pgvector + rag-mcp,推荐)——
make rag-stack-up      # 【生产模式】构建并启动整栈;rag-mcp 代码 COPY 进镜像、无热重载;首次创建共享网络 deer-flow-shared
make rag-stack-up-dev  # 【开发模式】同上,但 rag-mcp 源码 BIND-MOUNT + watchfiles 热重载(叠加 docker-compose.rag.dev.yaml)
make rag-stack-down    # 停止并删除整栈
make rag-stack-logs    # 跟踪 postgres + rag-mcp 日志
make rag-ingest-docker # 在栈内一次性入库(把 PTERO_RAG_DOCS_DIR 挂进容器)

# —— 仅 Postgres 单服务 ——
make pg-up          # 只起 RAG 栈里的 pgvector Postgres 服务
make pg-down        # 停止并删除 RAG 栈容器
make pg-restart     # 重启 Postgres 服务(保留数据卷)
make pg-destroy     # 停止并删除数据卷(破坏性,清空所有数据)
make pg-logs        # 跟踪 Postgres 日志

# —— RAG 扩展栈 + DeerFlow 主 dev 栈组合起停 ——
make stack-up       # 先起 RAG 栈(生产模式,建共享网络),再起 Docker dev 栈(Gateway/Frontend/Nginx)
make stack-up-dev   # 同上,但 RAG 栈用开发模式(watchfiles 热重载)
make stack-down     # 先停 Docker dev 栈,再停 RAG 栈
make stack-restart  # 重启 RAG 栈与 Docker dev 栈

# —— 向量库操作(宿主机进程,本地调试)——
make rag-ingest     # = pterodactyl-rag ingest(连 localhost:5432)
make rag-serve      # = pterodactyl-rag serve
make rag-stats      # = pterodactyl-rag stats
make rag-reset RAG_RESET_YES=1   # = pterodactyl-rag reset --yes(有守卫,须显式带 RAG_RESET_YES=1)
```

`rag-*`(宿主机)目标从 **你的环境变量** 读取密钥和文档目录(绝不写进 Makefile),运行前先设好:

```bash
export PTERO_RAG_EMBED_API_KEY="$OPENAI_API_KEY"   # 或本地服务用占位串 "local"
export PTERO_RAG_DOCS_DIR="/绝对路径/docs"          # rag-ingest 必需
# PTERO_RAG_DATABASE_URL 默认就是 docker-compose.rag.yaml 里 postgres 的 DSN(localhost:5432),连别的库时再 export 覆盖
```

> 容器化路径与宿主机路径的区别:`make rag-stack-up` 让 rag-mcp **跑在容器里**,用服务名 `postgres` 连库、
> 用 `host.docker.internal` 连宿主机 Ollama;`make rag-ingest` / `rag-serve` 等 `rag-*` 目标是 **宿主机进程**,
> 连 `localhost`,供本地快速调试。两者共用同一个 pgvector 数据库。

## MCP 工具

| 工具                                                                 | 用途                                                                                                           |
|--------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------|
| `rag_search(query, top_k, plugin, category, platform, lang, tags)` | 语义检索;`category`/`lang` 为 schema 枚举,`plugin`/`tags` 做模糊归一化;返回排序片段 + `filter` 回显块(`applied`、`relaxed`、`note`)。 |
| `rag_get_document(source_path, max_chars)`                         | 返回某个已索引来源的完整(有界)文本。                                                                                          |
| `rag_list_sources(plugin)`                                         | 列出已索引文档及其 plugin/title/chunk 数。                                                                              |
| `rag_list_facets(namespace)`                                       | **发现工具**:按命名空间列出索引中真实存在的标签取值及文档数。                                                                            |
| `rag_stats()`                                                      | 索引健康:文档/分块数、嵌入模型/维度、最近入库时间。                                                                                  |

### agent 如何使用标签

标签存起来很便宜,难点在于让 agent 挑对 **过滤值**。这里的设计遵循业界成熟 RAG 的共识:
**发现优于猜测**(`rag_list_facets` 返回真实插件名)、**闭集用枚举**(`category`/`lang`)、
**软过滤**(错误标签退化为噪声更大的结果,而非空结果)、以及 **服务端模糊归一化**
(`EssentialsX` → `plugin:essentialsx`)。`minecraft-server-ops` 技能里编码了这套流程:
*先调 `rag_list_facets` 确认插件名,再带上 `plugin=` 调 `rag_search`。*

## 部署到 DeerFlow

**推荐路径:容器化 RAG 扩展栈 + 共享网络。** 所有服务都跑在 Docker 里,无需在宿主机常驻任何
Python 进程。栈由 `docker/docker-compose.rag.yaml` 定义,包含两个容器:

- **`postgres`** — pgvector 镜像,同时承载 RAG 向量库(独立 `pterodactyl_rag` schema)与(可选)
  DeerFlow 自身持久化。
- **`rag-mcp`** — 本包容器化后的 MCP server,以 streamable-http 监听 `0.0.0.0:8000`。

两个容器都挂在共享外部网络 `deer-flow-shared` 上;DeerFlow 主 dev 栈
(`docker-compose-dev.yaml`)的 gateway 也加入这张网络,于是 **用服务名直连**
`http://rag-mcp:8000/mcp`。嵌入服务(Ollama)**留在宿主机**,rag-mcp 通过
`host.docker.internal:11434` 连它(Linux 下由 `extra_hosts` 映射到宿主网关)。

先理清三个角色,后面的步骤就不会绕:

- **`ingest`(入库)** — 离线一次性命令,把文档灌进数据库。容器化后用
  `make rag-ingest-docker`(把 `PTERO_RAG_DOCS_DIR` 挂进容器);只需在文档更新时重跑。
- **`serve`(检索进程)** — 现在是 `rag-mcp` **容器**,由 `make rag-stack-up` 拉起并常驻,
  agent 通过它调那 5 个工具。不再作为 Gateway 子进程。
- **`extensions_config.json` 里的那段配置** — 告诉 Gateway「这个 HTTP MCP server 在哪、
  什么时候优先用它」。把 `enabled` 改成 `true` 只是让 Gateway 去连它,
  **不会替你入库**——空索引查不到东西,所以务必先做第 3 步再开第 4 步。

### 1. 起 pgvector Postgres(RAG 扩展栈的一部分)

本包需要一个装了 **`pgvector` 扩展** 的 Postgres。索引建在独立的 `pterodactyl_rag`
schema 里,不会污染其它表。`docker/docker-compose.rag.yaml` 里的 postgres 服务用
`pgvector/pgvector:pg16` 镜像,首次启动自动 `CREATE EXTENSION vector`。

整栈一起起(推荐,连 rag-mcp 一并拉起):

```bash
make rag-stack-up      # 【生产模式】构建并启动 pgvector + rag-mcp;首次创建共享网络 deer-flow-shared
make rag-stack-logs    # 看日志确认就绪(rag-mcp 应打印 Uvicorn running on http://0.0.0.0:8000)
# 收工:make rag-stack-down
```

> **开发模式(热重载)vs 生产模式(代码拷入镜像)** —— 对齐 DeerFlow 自身的 dev/prod 约定
> (`make docker-start` vs `make up`)。rag-mcp 的 `Dockerfile` 是多阶段的:
>
> - `make rag-stack-up`(**生产**)—— 构建 `runtime` 阶段,`COPY . .` 把源码 **拷进镜像**、
>   `uv sync --frozen --no-dev`,无 watcher。镜像自包含,适合部署。
> - `make rag-stack-up-dev`(**开发**)—— 叠加 `docker/docker-compose.rag.dev.yaml`,构建 `dev`
>   阶段:把 `src/`、`pyproject.toml`、`uv.lock` **bind-mount** 进容器,并用 `watchfiles` 监听
>   `/app/src`,改文件即重启 server(FastMCP 的 `mcp.run()` 不暴露 uvicorn `--reload`,故用
>   watchfiles 包裹)。`.venv` 走命名卷 `rag-mcp-venv`,避免宿主机空目录盖住镜像里已装好的依赖。
>
> 两种模式共用同一个 `docker-compose.rag.yaml` 作为基座,dev 只是叠加一个 overlay 覆写
> `build.target` 与 `volumes`——就像主栈用 `docker-compose-dev.yaml` 覆写 prod 定义一样。

只想先起 Postgres 单服务(例如先入库):

```bash
make pg-up          # 只起 postgres 服务(容器名 deer-flow-postgres,端口 5432)
```

> ⚠️ **DeerFlow 默认并不跑 Postgres。** 它默认用 **SQLite**(`.deer-flow/data/deerflow.db`)。
> RAG 扩展栈自带的这个 pgvector 只服务本包;若想让 DeerFlow 主程序也复用它,见下方可选步骤。

连接串(DSN)——容器内与宿主机主机名不同:

```
postgresql://deerflow:deerflow@localhost:5432/deerflow   # 宿主机访问(make rag-ingest 等)
postgresql://deerflow:deerflow@postgres:5432/deerflow    # 网络内访问(rag-mcp 容器,用服务名)
```

(可用项目根 `.env` 的 `POSTGRES_USER/PASSWORD/DB/PORT` 覆盖。)

**(可选)让 DeerFlow 主程序也用这个库**——本包不读 `config.yaml`,这一步只是把 DeerFlow
自己的持久化从 SQLite 切到同一个 Postgres,与本包共库但 **不撞车**(本包所有表都在
`pterodactyl_rag` schema,DeerFlow 用默认 public schema)。在项目根 `.env` 与 `config.yaml` 里:

```bash
# 项目根 .env
DATABASE_URL=postgresql://deerflow:deerflow@localhost:5432/deerflow
UV_EXTRAS=postgres        # 让 make dev / docker 装上 postgres driver
```

> ⚠️ **本地起 vs Docker 起,主机名不同。** 上面的 `localhost` 适用于 `make dev`(gateway 
> 直接跑在宿主机上)。但 `make stack-up` / `make docker-start` 会把 gateway 跑在 **容器** 里,
> 容器内的 `localhost` 指向容器自身,连不到宿主机上发布的 Postgres——此时 `.env` 里的
> `DATABASE_URL` 必须改用 `host.docker.internal`:
>
> ```bash
> DATABASE_URL=postgresql://deerflow:deerflow@host.docker.internal:5432/deerflow
> ```
>
> 本包的 `PTERO_RAG_DATABASE_URL` 同理:MCP server 若被 Docker 里的 gateway 作为子进程拉起,
> 它引用的 `$DATABASE_URL` 也应指向 `host.docker.internal`。

```yaml
# config.yaml
database:
  backend: postgres
  postgres_url: $DATABASE_URL
```

首次可先手动 bootstrap driver:`cd backend && uv sync --all-packages --extra postgres`。
若你只是想让本包用 Postgres、DeerFlow 主程序继续用 SQLite,跳过这一步即可——本包只认
下面的 `PTERO_RAG_DATABASE_URL`。

> 如果复用的是一个 **已有的、没装 pgvector** 的 Postgres,需在库里执行一次
> `CREATE EXTENSION IF NOT EXISTS vector;`(需相应权限)。

### 2. 安装这个包

它是一个 **独立** uv 项目(不在 harness 的 workspace 里),在包目录内单独安装:

```bash
cd backend/packages/pterodactyl-rag
uv sync                     # 按 uv.lock 装好全部依赖
uv run pterodactyl-rag --help   # 验证 `pterodactyl-rag` 命令可用
```

后续所有 `pterodactyl-rag ...` 命令都在 **这个目录** 下用 `uv run pterodactyl-rag ...` 执行
(下文为简洁省略了 `uv run` 前缀)。

### 3. 先入库一次(必须,先于启用)

把要检索的插件文档放进一个目录,例如按 `插件名/类别/文件.md` 组织(这样标签能自动推断):

```
docs/
  EssentialsX/
    config/kits.md
    permissions/nodes.md
```

设好环境变量再入库(密钥用真实值或 `$VAR`,**不要写进配置文件**):

```bash
export PTERO_RAG_DATABASE_URL="postgresql://deerflow:deerflow@localhost:5432/deerflow"
export PTERO_RAG_EMBED_API_KEY="sk-..."      # 或 $OPENAI_API_KEY
export PTERO_RAG_DOCS_DIR="/绝对路径/docs"

pterodactyl-rag ingest       # 切片 → 向量化 → 写入,并清理已删除文档
pterodactyl-rag stats        # 确认:documents / chunks 数量 > 0
```

`stats` 显示文档数和分块数大于 0,就说明索引就绪了。文档有变动时重跑 `ingest` 即可
(未变的文档会按 SHA-256 自动跳过)。

#### 用本地模型跑向量化(无需 OpenAI key)

嵌入器只认「OpenAI 兼容的 `/embeddings` 端点」,所以任何暴露该接口的本地服务都能直接对接,
模型权重从 HuggingFace 拉取,**不走外网 API**。key 本地服务不校验,填个占位串即可:

```bash
# 方式一:Ollama(OpenAI 兼容端点在 /v1)
ollama pull nomic-embed-text            # 也可 bge-m3 / mxbai-embed-large
ollama serve                            # 默认 http://localhost:11434
export PTERO_RAG_EMBED_BASE_URL="http://localhost:11434/v1"
export PTERO_RAG_EMBED_MODEL="nomic-embed-text"
export PTERO_RAG_EMBED_DIM=768          # ← 必须等于所选模型的真实维度
export PTERO_RAG_EMBED_API_KEY="local"  # 占位符,本地服务不校验

# 方式二:HuggingFace text-embeddings-inference(TEI)
docker run -p 8080:80 ghcr.io/huggingface/text-embeddings-inference:cpu-latest \
  --model-id BAAI/bge-small-en-v1.5
export PTERO_RAG_EMBED_BASE_URL="http://localhost:8080/v1"
export PTERO_RAG_EMBED_MODEL="BAAI/bge-small-en-v1.5"
export PTERO_RAG_EMBED_DIM=384          # bge-small=384、bge-m3/mxbai=1024、nomic=768
export PTERO_RAG_EMBED_API_KEY="local"
```

> **维度必须写对。** 本地模型的维度通常不是 OpenAI 的 1536(如 bge-small=384、
> nomic-embed-text=768、bge-m3=1024)。维度在首次入库时被 **锁进索引**,所以从别的模型
> 切过来必须先 `pterodactyl-rag reset --yes` 清空,再用新的 `PTERO_RAG_EMBED_DIM` 重新 `ingest`。

### 4. 在 `extensions_config.json` 里启用(HTTP,连容器)

容器化路径下,`rag-mcp` 已经是一个常驻的 HTTP MCP server;Gateway 只需 **连过去**,
不再自己拉起进程。在仓库根目录的 `extensions_config.json` 的 `mcpServers` 下,把对应
条目的 `enabled` 改成 `true`(可直接启用的样例见 `extensions_config.example.json`):

```jsonc
"pterodactyl_rag": {
  "enabled": true,                 // ← 改成 true,Gateway 才会去连这个 server
  "type": "http",                  // 传输:streamable-http
  "url": "http://rag-mcp:8000/mcp", // 共享网络里用服务名直连 rag-mcp 容器
  "headers": {},
  "tool_call_timeout": 60,         // 单次工具调用超时(秒)
  "routing": {                     // 路由提示:命中这些关键词时优先用本 server 的工具
    "mode": "prefer",
    "priority": 60,
    "keywords": ["plugin", "插件", "config", "permission", "EssentialsX", ...]
  }
}
```

- `url` 用 **服务名** `rag-mcp`,靠共享网络 `deer-flow-shared` 解析——所以务必先
  `make rag-stack-up`(创建网络 + 起 rag-mcp),再起主栈(`make docker-start` 或 `make stack-up`)。
- 密钥(DSN / 嵌入 key)**不在这里配**:它们已作为 `rag-mcp` 容器的环境变量注入
  (见 `docker-compose.rag.yaml`,只用 `$VAR` 引用根 `.env`,绝不内联、绝不打日志)。
- `routing` **只影响** 工具的路由权重(何时优先暴露给 agent),不决定连不连。

改完 `enabled: true` 后重启 / 重载 DeerFlow(`make docker-restart`),agent 就能用上
`rag_search` 等工具了。无需改动任何 harness 代码。

### 5.(替代)本地调试:stdio 子进程(不走 Docker)

不想起容器、只在宿主机快速调试时,可让 Gateway 把本包作为 **stdio 子进程** 拉起。
把配置条目改成:

```jsonc
"pterodactyl_rag": {
  "enabled": true,
  "type": "stdio",
  "command": "pterodactyl-rag",    // 需在 Gateway 进程的 PATH 上
  "args": ["serve"],
  "env": {                          // 只用 $VAR 引用,绝不内联密钥
    "PTERO_RAG_DATABASE_URL": "$DATABASE_URL",
    "PTERO_RAG_EMBED_API_KEY": "$OPENAI_API_KEY"
  },
  "tool_call_timeout": 60
}
```

- 此路径要求 `pterodactyl-rag` 命令在 Gateway 进程的 PATH 上(用 `uv` 装的话指向对应
  可执行文件,或确保 venv 已激活),且宿主机上设好了被 `$VAR` 引用的变量。
- 若 Gateway 本身跑在 Docker 里,子进程连库的 `$DATABASE_URL` 应指向 `host.docker.internal`
  而非 `localhost`(容器内 `localhost` 是容器自身)。这也是为什么容器化部署更省心——
  服务名 `rag-mcp` / `postgres` 天然可解析,不必区分主机名。

### 常见问题排查

- **`rag_search` 总是查不到东西** → 多半是没入库或入库到了另一个库。回到第 3 步跑
  `stats` 确认文档数 > 0,并核对 `PTERO_RAG_DATABASE_URL` 与配置里的 `$DATABASE_URL` 指向同一个库。
- **server 启动即报 `ValueError`(维度/模型不一致)** → 索引里锁定的嵌入维度/模型和当前配置
  对不上。要换模型或维度,须先 `pterodactyl-rag reset --yes` 清空索引再重新 `ingest`。
- **`rag_search` 返回「no embedding API key」提示** → `PTERO_RAG_EMBED_API_KEY` 没配到;
  此时发现类/统计类工具仍可用,但语义检索需要 key(或本地服务的占位串)才能把查询向量化。
- **用本地模型时报维度不一致 / 检索结果异常** → `PTERO_RAG_EMBED_DIM` 和本地模型的真实维度
  对不上(如把 nomic-embed-text 当成 1536)。改对维度,先 `reset --yes` 再重新 `ingest`。
- **`make rag-*` 报 `PTERO_RAG_* is required`(明明 `.env` 里配了)** → `make` / `uv run`
  **不会** 自动加载项目根 `.env`(只有 docker compose 和 DeerFlow 应用会)。`rag-*` 目标已在
  运行前自行 `source .env`;若仍缺变量,确认变量确实写在项目根 `.env`、或用
  `ENV_FILE=/path/to/env make rag-ingest` 指定其它文件。
- **入库报 `Unknown scheme for proxy URL 'socks://...'`** → shell 里设了全局 `ALL_PROXY=socks://`,
  httpx 构建 transport 时会因缺 `socksio` 而失败。本地嵌入端点(`localhost`/`127.0.0.1`)
  现在会 **自动忽略** 环境代理(`trust_env=False`);若你的嵌入端点是远程域名又必须走 socks
  代理,则需另装 socks 支持或改用 http(s) 代理。
- **测试或 server 报 `No module named 'mcp.server.fastmcp'` / `psycopg_pool`** → 依赖没装全或
  解析到了不兼容版本。`mcp` 锁定在 `>=1.2.0,<2`(2.0.0 是破坏性重写,移除了 FastMCP),
  `psycopg` 需带 `pool` extra(连接池)。在包目录重跑 `uv sync` 即可对齐。
- **切到 Postgres 后网关容器崩溃 / nginx 502,报 `asyncpg is not installed`** → `config.yaml`
  设了 `database.backend: postgres`,但 Docker 网关容器没装 postgres driver。docker 的
  entrypoint 只在 `UV_EXTRAS` 含 `postgres` 时才装 `asyncpg`——在项目根 `.env` 里加
  `UV_EXTRAS=postgres`。**改完 `.env` 必须用 `make docker-start` 重建容器**(不能用
  `make docker-restart`:`docker compose restart` 只重启进程、不重读 `env_file`,改动不生效)。

## 开发

```bash
cd backend
PYTHONPATH=packages/pterodactyl-rag/src uv run python -m pytest packages/pterodactyl-rag/tests/ -q
```

测试使用确定性的假向量器和内存存储,无需真实数据库或嵌入 API。
`test_end_to_end_smoke.py` 会跑通部署时的完整链路——从磁盘加载文档 → 切片/打标签/向量化 →
写入 → `rag_search` → `rag_get_document` 并引用一个配置项——用的是同一对测试夹具,
因此模块之间任何一处断裂都会表现为「引用失败」。真实 Postgres + 真实嵌入的冒烟测试
(入库一批文档、以 stdio 启动 server、让 Gateway agent 去 `rag_search`)是一个手动步骤:
把 `PTERO_RAG_DATABASE_URL` / `PTERO_RAG_EMBED_API_KEY` 指向真实后端,再依次运行
`pterodactyl-rag ingest` 和 `pterodactyl-rag serve`。完整设计见
`docs/plans/2026-07-29-pterodactyl-rag-mcp-design.md`。

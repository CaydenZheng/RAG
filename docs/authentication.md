# 账号、登录会话与历史（AUTH-USER-1～4／U1 候选）

## 阶段与适用边界

本阶段提供邀请制账号认证、RAG/Agent 所有权、历史目录和网页登录候选，仍须统一人工 Review，不是已批准上线的多用户产品。仅允许受控本机实验；
`AUTH_ENABLED=true` 时旧业务路径要求账号并使用账号作用域；关闭时仅保留原本地匿名兼容行为。
**所有权、AUTH-USER-4 的共享资源权限与网页集成通过前，不得多人/公网开放后端。**
启用认证时提供登录、强制初始改密、退出和账号切换；没有公开注册、邮箱、SSO、个人 MCP OAuth 或私有知识库。
共享知识库、索引及 Provider 仍由应用拥有，管理员密钥不保存在浏览器，账号 admin 不自动获得既有管理员接口权限。

## 账号、密码与登录会话合同

- 账号通过本地受控命令创建，稳定 UUID 不重用；用户名为大小写不敏感的 ASCII `[a-z0-9][a-z0-9_.-]{2,63}`，角色仅 admin/member。
- 密码为 12～128 字符，不允许 NUL；Argon2id v19，64 MiB、3 次迭代、4 路。使用锁定的 `argon2-cffi==25.1.0`（bindings 由锁文件固定），不自写密码算法。不存在账号也执行一次同配置验证，不承诺网络级恒定时间。
- 管理员创建或重置的密码必须先修改。`/auth/me`、修改密码和退出可使用初始登录会话；`require_user`/`require_admin` 在改密前拒绝业务授权。
- 32 随机字节登录凭证通过 Cookie `ragflow_login` 签发；数据库仅存 SHA-256 摘要。8 小时绝对到期，无滑动续期；每账号最多 10 个有效登录会话，新登录淘汰最早项。当前浏览器重新登录会撤销其携带的旧会话并换 Cookie，不能指定新 token。
- 退出只撤销当前会话；用户改密撤销全部登录会话（含当前），须重新登录；管理员重置、停用或重新启用均递增凭据版本并撤销全部会话。重置不自动重新启用停用账号。
- 验证密码期间发生重置/停用，会在写入登录凭证前重查版本与启用状态；改密也在事务内重查会话和版本，避免退出/重置后提交旧操作。
- 同一用户名每 5 分钟 10 次、同一来源地址每 5 分钟 30 次认证尝试，成功也计数；改密验证共用账号预算。限速窗口进程内保存，重启归零；最多 4096 个有效计数桶，满载拒绝而不驱逐活跃桶。最多同时 2 次密码运算，满载返回 503；此为本机默认值，不是压测结论。
- 凭证、密码、CSRF token 不进入 URL、日志、Trace 或 localStorage。响应与错误均 `Cache-Control: no-store`。没有账号与错误密码/停用账号返回同一 401；422 不回显输入。

## HTTP 接口

所有 `/auth/*` 接口在认证未启用或服务未初始化时返回脱敏 503。请求不接受查询串；登录与改密仅接受最大 4096 字节的 JSON，拒绝重复/额外字段。没有注册或网页账号管理端点。

| 方法与路径 | 请求与前置 | 结果 |
|---|---|---|
| POST `/auth/login` | `{username,password}`；允许的 Origin | 200 `{user:{id,username,role,must_change_password},csrf_token}`；设置登录 Cookie |
| GET `/auth/me` | 有效登录 Cookie | 200 同一用户投影与当前会话 CSRF token；不会返回登录 token |
| POST `/auth/logout` | 允许的 Origin + `X-CSRF-Token` | 204，撤销当前凭证并清 Cookie；对相同旧凭证幂等，不授予其他权限 |
| POST `/auth/password` | `{current_password,new_password}`；允许的 Origin + `X-CSRF-Token` + 有效会话 | 204，全会话失效并清 Cookie；新旧密码须不同 |

正常 Cookie 为 `HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=28800`，不设置 Domain。
CSRF token 由登录随机凭证通过 HMAC-SHA256 固定用途标签派生，不能单独用于登录；前端只在内存使用，刷新后通过 `/auth/me` 重新取得。
状态修改检查精确 Origin 白名单，缺少、null、重复、恶意后缀或 `Sec-Fetch-Site: cross-site` 拒绝；登录也检查 Origin，避免登录 CSRF。SameSite 不是唯一防线。

默认只接受 HTTPS。开发 HTTP 必须显式 `AUTH_ALLOW_LOOPBACK_HTTP=true`，且 ASGI 看到的客户端与服务端地址都为 loopback。
精确配置 `AUTH_ALLOWED_ORIGINS`，禁止通配符、路径、凭据和查询串。HTTP origin 只允许 localhost/loopback IP；这不是允许内网 HTTP 密码登录。
未来通过 HTTPS 代理时，只信任明确代理地址的 scheme/IP 转发头，不信任任意 `X-Forwarded-*`，不得开启 `--forwarded-allow-ips='*'`。不配置泛域 CORS。
认证路径访问日志会剥离查询串作为纵深防护；代理端也不得记录凭据、Cookie、请求/响应正文或认证查询串。不要依靠拒绝查询串来阻止代理提前记录。

## 路由与资源权限矩阵

下表区分账号模式已接入的路径与后续门禁；关闭认证不是公网部署方案。

| 现有路径/资源 | 本阶段实际状态 | 后续权限合同及归属 |
|---|---|---|
| `/`、`/agent`、`/static/app/*` | 匿名页面及资源，维持严格 CSP | 页面壳可匿名，私有 API 要求账号；AUTH-USER-4 |
| `/health` | 匿名最小存活 | 保持最小公开 |
| `/workspace/config` | 最小公开开关 `auth_enabled`，no-store | 配置读取失败不得退回匿名模式 |
| `/ready` | 账号模式要求登录；匿名兼容保留原状态 | 无秘密的依赖投影，不用模型付费探活 |
| `/docs`、`/redoc`、`/openapi.json` | 账号模式需原管理员密钥 | 账号 admin 不替代 `X-Admin-Key` |
| `/auth/login` | Origin/传输/限速保护，无需已有账号 Cookie | 无公开注册 |
| `/auth/me`、`/auth/password`、`/auth/logout` | 本单元生效的认证及 CSRF 合同 | 同一登录会话，不接受客户端 owner |
| POST `/query`、GET `/query/stream`、POST `/agent/chat`、`/agent/chat/stream` | 账号模式要求登录、改完初始密码及 CSRF；按账号隔离 Memory/缓存 | 共享工具允许清单与额度 AUTH-USER-4 |
| GET `/session/{id}`、POST `/session/reset`、GET `/agent/memory/{id}`、POST `/agent/reset` | 账号模式要求 owner，未知/其他 owner 均 404；旧投影仍非完整 Transcript | 历史 UI 不用截断 Memory 或破坏性 reset 作新建 |
| GET/POST `/agent/approvals/*`、`/agent/elicitation/*` | 当前账号与会话 owner；响应附 Origin/CSRF，不能跨账号读取/批准 | 仍经 Registry Policy/Schema/审批/审计，无前端直调 |
| GET `/agent/tools` | 账号模式要求登录，member 按有效策略过滤 | available 不是授权；共享 MCP 默认不允许 |
| POST `/upload`、DELETE `/documents/{filename}`、POST `/index/rebuild`、`/index/rollback` | 现有 `X-Admin-Key`（或显式本机开发免认证） | 仍仅受控管理员，不因 role=admin 放行；AUTH-USER-4 复核 |
| GET `/index/jobs/{job_id}` | 账号模式要求管理员密钥 | 不因普通登录或 admin 角色自动放行 |
| GET `/agent/oauth/{id}`、POST `/agent/oauth/{id}/cancel` | 现有管理员密钥 | 仍应用级管理，不向普通账号开放 |
| GET `/agent/oauth/{id}/callback` | 公开 Provider 回调，已有 state 校验 | 保持 SDK/state 防护，不当作登录；独立凭据体系 |
| `/conversations*` | 仅账号模式向当前 owner 提供目录、分页、回看、重命名、清空与删除 | 未登录/认证关闭均 401，非 owner 404，私有响应 no-store |
| 账号创建/启停/重置 | 仅可信本地 CLI，没有 HTTP 接口 | 不借用浏览器管理员密钥 |

账号所有权、记忆/缓存隔离与在途撤销有离线证据，仍须独立 Review；共享资源细化授权、网页账号切换、完整跨设备历史**尚未验收**。
`ragflow_client` 与浏览器 localStorage 的 session ID 均不是用户身份，旧匿名数据不会自动认领或迁移。

## 配置、管理与回滚

默认：`AUTH_ENABLED=false`、`AUTH_DB_PATH=./data/auth.db`、`AUTH_ALLOWED_ORIGINS=[]`、`AUTH_ALLOW_LOOPBACK_HTTP=false`。
导入 app 或关闭认证时不创建认证库；启用后完整 lifespan 的认证 startup 初始化，失败阻止启动，不跳过 lifespan。
本阶段没有要求配置真实密码或启动本地账号服务，不应为测试修改 `.env` 或生产库。

可信本地管理员在仓库根目录使用指定虚拟环境并设置 `UV_PROJECT_ENVIRONMENT`；密码由安全终端 getpass 双次输入，不接受命令行、环境变量或回显 stdin。

```powershell
$env:UV_PROJECT_ENVIRONMENT = 'C:\Users\beibe\Desktop\实习工作记录\TTS调研\.venv'
uv run --no-sync --offline --no-env-file python -m scripts.manage_users --db ./data/auth.db create alice --role member
uv run --no-sync --offline --no-env-file python -m scripts.manage_users --db ./data/auth.db reset-password alice
uv run --no-sync --offline --no-env-file python -m scripts.manage_users --db ./data/auth.db disable alice
uv run --no-sync --offline --no-env-file python -m scripts.manage_users --db ./data/auth.db enable alice
```

这些命令是真实账号操作示例，不是自动运行步骤。私下分发临时密码，首次登录必须改密；无邮件找回承诺。
设置认证文件与备份的 OS ACL 仅允许服务账号/可信管理员读取，不能与静态资源同目录。自定义路径也须忽略 Git。

认证采用独立 SQLite 文件，`user_version=1`，`auth_users`、`auth_sessions` 与用户索引在单事务初始化。账号模式的显式 startup 另在原 `sessions.db` 追加 `conversations` 和 `schema_migrations`，保留原会话表、epoch 和全部匿名记录。
不是新建空库或已识别认证 schema 的文件会拒绝初始化；更高 schema 版本同样拒绝。未提供认领或删除旧匿名数据的迁移。
本阶段只在临时库验证迁移；首次实际启用前确认两个 SQLite 的绝对路径和权限，停服务并备份后再执行版本变更。不要为运行测试修改 `.env` 或真实库。
回滚先停止外部监听与账号入口，再恢复已审代码；不要 DROP 表或删除认证库、所有权表。关闭认证只恢复本机匿名兼容，不能用于多人服务；若需要恢复数据须另行授权。

## 会话所有权与在途撤销（AUTH-USER-2）

- `conversations` 按 `(owner_user_id,public_id)` 唯一，保存模式与服务器内部 storage_id；为 U1 共用基础表，不创建第二套会话目录。只在账号模式 startup 执行版本化 additive 初始化，不在默认匿名导入时迁移。首版每账号最多 200 个非删除项。
- 旧生成入口首次使用当前账号的新 ID 时建立归属；回看/reset/pending 只查已存在归属，不创建或认领。其他账号相同公开 ID 是不同资源；同 owner 已存在 ID 不可换模式。内部键使用独立 `u_` 账号命名空间，不与匿名键混用，存储层也校验 owner。
- 相同账号换设备使用同一 stable user_id；匿名 cookie 和客户端 owner 字段不授予权限。此阶段仅验证旧 Memory 回看/继续，不声称有新目录、完整 Transcript 或压缩原文恢复。
- 长期记忆只读 `memory/users/{account_scope}/long_term.md`，不存在返回空；不读全局/匿名 fallback，不自动迁移旧 JSON。私有 LLM cache 强制使用账号维度，显式传其他维度不能获得另一账号缓存；检查权限先于缓存读取，撤销后不新发 Provider/Ollama 调用。
- 每个账号会话只允许一个在途生成，另一请求 409；不同会话可并行。进程内索引只记录活跃请求，完成/取消后释放，不是持久任务队列。
- 每次业务请求重新认证；在途请求每 250ms 复查登录凭证，退出/停用/重置/过期后取消执行与 reader，等待中的审批/Elicitation 随调用 finally 关闭。reset 立即标记并取消该会话在途运行，原 epoch 保护继续生效。同步工具或远端已开始的副作用不能承诺回滚，未知结果不重放。
- 终态和响应完成以 ASGI `send` 返回为准，不是客户端已读到的保证。发送受阻期间撤销，未送出的 SSE 终态用一个稳定撤销 ERROR 结束；已接受终态则不再追加。取消发生在 send 时也显式关闭底层迭代器。普通 JSON 在响应头已接受、body 未完成时以异常中止连接，不能改状态码或伪造空 body；客户端应按未确认处理，不自动重发。
- 最终提交复用现有非阻塞 SQLite COMMIT 重试；每次尝试持有短认证读事务，与包括 CLI 在内的凭证撤销形成明确先后顺序；历史库忙锁重试间释放认证读锁，避免阻挡退出。撤销先确认后旧历史不能提交；提交先完成则记录保留，不伪称撤销外部副作用。
- 异步 Memory 保存另以每次请求独立的 `CommitPermit` 排序取消确认和单次非阻塞 COMMIT：取消先确认则后续写入回滚；提交尝试先取得许可锁，则等待这一次尝试结束后才确认取消，已提交记录保留。许可锁不跨压缩、认证读取或 BUSY 重试等待；deadline、epoch/revision 与认证资格继续校验。内部普通 `can_commit` 回调仅作资格快照；需要确认取消顺序时必须传同一个许可对象并用它撤销，不能包装成独立布尔回调。
- POST 业务接口检查 Origin/CSRF。`GET /query/stream` 虽仍用 fetch GET SSE，但会花费预算/保存历史，必须附 `X-CSRF-Token`；同源 GET 可不带 Origin，若带则精确校验，并拒绝跨来源 fetch。访问日志剥离其查询串；代理也须禁记问题查询串，后续 POST 迁移另审。
- 本阶段普通 member 的共享 MCP 默认 fail closed；只有服务器 `MEMBER_MCP_ALLOWLIST` 精确注册名允许项才可能调用。允许清单不会绕过 Registry Policy、Schema、审批及审计；admin 也不获得管理员 HTTP 密钥的替代能力。
- 工具审计/Trace 只增加单向账号 scope，不写原始账号凭据、参数值或远端结果。旧匿名数据原地保留，无自动到期、认领或清理任务；删除/迁移须独立授权，不能靠旧公开 session ID 转移数据。

## 目录、完整回看与新建（U1／AUTH-USER-3＋4）

- 复用 owner `conversations`，显式启动增加 `transcript_exchanges`、目录 `last_seq` 和迁移版本。epoch 仍只来自 `session_state`，不另存一份。原匿名数据保留，不认领、不清理；旧压缩掉的内容无法恢复。
- `/conversations` 创建使用浏览器 UUID，重复同 ID/模式返回原记录；目录按更新时间/ID 倒序、按模式筛选，默认 20、最大 50；游标不超过 512 字节。标题搜索为 POST body，不在 URL 放标题。
- 人类回看的每轮完整问答与 Memory 追加/压缩、目录更新在同一 SQLite 事务提交，沿用认证读事务、epoch/CAS 和取消许可。摘要计算不持事务/许可锁，不删除 Transcript；目录摘要不存工具参数、远端原始结果或内部规划。
- 完成回合的已知 `used_model` 在同一事务更新目录 `last_model`；后者表示最近一次已知实际模型。未调用模型的确定性拒答在该轮记录 null，并保留目录原值；从未调用则仍为 null。提交失败/取消不更新，清空保留该字段。它不代表尚未实现的模型切换偏好。
- 首条完成问题压缩空白、取 80 Unicode 码点作标题；用户可改名。每账号 200 个活跃会话、每会话 2000 个完成回合；达到上限返回冲突，不静默淘汰。
- 新建先停止当前流与轮询，创建成功后才切新 ID，保留旧历史；创建失败保留原页。清空与删除须独立确认，条件版本冲突不自动重试。清空保留目录/标题/最近模型，增加 epoch；删除保留不可复活 tombstone，后续读取 404。当前没有附件或 Skills，空字段不是可用能力。
- 新浏览器每轮发送 `client_turn_id`；同会话在途/已提交重复 409，模型和工具不会被自动重跑。新旧 conversation/session ID 同时出现但不同则 422。旧客户端未提供轮次 ID 时仅作兼容，无法提供丢包精确核对。
- durable commit 后才发 DONE，但网络丢失 DONE 不会回滚已提交问答。界面断连/协议异常先保持“未确认”，显式按 `GET /conversations/{id}/turns?client_turn_id=UUID` 核对；查无/失败仍未确认，禁止自动重发。该查询不与显式 limit/before 混用；停止不撤销工具已经发生的副作用。
- 登录凭据为 HttpOnly Cookie，CSRF 只在页面内存；账号历史、密码、私有正文不放 localStorage。刷新按当前 owner 和 URL 的公开对话 ID 重取；退出/切号/过期卸载两个模式并取消 reader、轮询和历史查询。跨标签页仅广播无数据的账号变化通知，不广播凭据或消息。
- Pages 不启用账号入口，不连接 `/auth`、`/conversations` 或私有后端，仍为明确的内存模拟。

## 共享资源与有界成本保护

账号模式下只开放共享知识库读取，网页不提供索引写或管理员密钥配置。共享 MCP 必须服务器逐工具允许，不能因名称/分类猜测安全；允许后仍经 Registry 全链路。OAuth 回调保留原 state/SDK 校验，其他管理路径及索引任务状态要求原 `X-Admin-Key`。关闭 AUTH 只恢复原本机兼容，不是降低公网门禁。

默认每账号生成请求 30 次/分钟、并发 2；每来源 120 次/分钟、并发 8。来源仅使用已验证的 ASGI client，不自行相信任意转发头；Vite/反代后的来源可能共享配额。请求窗口仅单进程内存，重启清空；表大小有界，满载拒绝，不驱逐活跃许可。

每日模型预算为持久 SQLite 预留：每账号 200000、每来源 1000000 units，UTC 日切换；按输入 JSON UTF-8 字节和输出上限、SDK 最大重试次数保守预留。覆盖同步、异步、native、流、摘要、嵌套检索和 Ollama；缓存命中、失败及取消也不退款，没有输出上限的账号调用固定 1024。它是保护阈值，不是实测 token 数、账单或金额承诺。预留忙锁立即失败，不能阻塞事件循环等待写锁后再发付费请求。超限用稳定 429/流 ERROR，不记录私有参数。实际适用额度须管理员按模型/设备能力调整，尚无生产压测或真实费用数据。

首次启用前须停服务、核对两个库的绝对路径并备份；功能验收应只在合成临时库迁移。实际库的启用与恢复须另行授权；回滚不得直接 DROP 新表，应停写后恢复经验证的代码/备份组合。历史功能启用前的匿名/已压缩内容不承诺恢复。

## 验证范围

永久离线测试使用合成账号与临时 SQLite，覆盖初始改密、哈希与凭证摘要、失效/撤销、Cookie、CSRF/Origin、枚举防护、限速/容量、版本竞争、schema 防误用、日志/错误脱敏和默认禁用。
AUTH-USER-2 已获用户认可，当前合并单元 U1／AUTH-USER-3＋4 仍须统一人工 Review。离线真实 SQLite/认证测试、fake API 浏览器操作与真实 Provider/索引/MCP 联调分别记录；自动化不能替代 TLS 反代、真实外部服务或生产压测。

# SoC 集群栈安全、可靠性与可运维性审计报告

- 审计日期：2026-08-29（Asia/Singapore）
- 审计对象：CP5105 Phase 1 单节点 Slurm 栈（Lumilake + FlowMesh + lumid-gateway + PostgreSQL + MinIO + Redis）
- 工作副本：MacBook Air `/Users/lifeifei/NUS_MComp/CP5105/`
- 集群目标：NUS SoC Slurm，登录入口 `xlogin2.comp.nus.edu.sg`，运行时目录 `~/lakehouse/`、`~/src/`、`~/slurm/`
- 证据快照：job `771030` 是前一次精确版本全栈成功；加入 Slurm log `0600` 加固后的 job `771041` 为最终签收，已 `COMPLETED 0:0`。

> 状态说明：本文严格区分 **Completed（已完成且有证据）**、**In progress（已有本地实现但尚未完成全链路验证/提交）** 与 **Residual（仍需处理或接受的剩余风险）**。工作区中出现代码 diff 不等于修复已经部署。

## 1. 结论摘要

Phase 1 的功能性里程碑是真实的：历史 Slurm job `740372` 在一个 GPU 节点上运行过 PostgreSQL、MinIO、Redis、lumid-gateway、FlowMesh native workers 和 Lumilake，并完成 Q2-Trading-Text 端到端执行。但原始部署脚本是“研究原型可跑”状态，不适合在共享节点上按安全服务长期运行。

最严重的问题不是单独某个固定密码，而是以下链路组合：

1. FlowMesh 默认无认证；部署脚本试图设置的 `SERVER_BIND_HOST=127.0.0.1` 并不是 HTTP server 实际读取的变量，HTTP/gRPC 原配置可能监听所有网卡。
2. 新增的 `native` provider 允许请求者传入任意 `command` 和 `cwd`，随后由 supervisor 以 Slurm 作业用户身份直接 `Popen`。
3. 因而能访问 FlowMesh 管理 API 的调用者，可把“创建 worker”升级为宿主机账户权限下的任意代码执行。这组条件成立时应按 **Critical** 处理。

其余高优先级问题包括：共享节点 loopback 上的 PostgreSQL `trust` + bootstrap superuser、无认证 Redis、固定 MinIO/gateway 凭据、`set -x` 将凭据写入 Slurm stderr、文档承诺 restore 但没有恢复代码、timeout 信号清理不可靠、静态 `/healthz`、宽泛 `pkill -f`、GPU allocation 可见性和 stop-failure 资源释放错误，以及两个历史验证脚本的退出码/控制流缺陷。

截至本证据快照，修复已从“本地实现”推进到 GitHub 与 SoC 全栈实机验证：

- **Completed / GitHub**：FlowMesh HEAD `0b8be48e711515e566a5125ff56b67166a997473`（[PR #1](https://github.com/ff98li/FlowMesh/pull/1)）、Lumilake `38ba3e503f4e131c29a2c8e8ee510108f0f59af3`（[PR #1](https://github.com/ff98li/Lumilake/pull/1)）与 analytics-agent 已部署 operational asset revision `673a0531fa55dbc7e9cd809a0fa002f84c0c60de`（[PR #1](https://github.com/ff98li/analytics-agent/pull/1)）已 push。FlowMesh 当前 server suite 为 **625 passed**；控制面主修复曾通过全套 **702 passed** 基线，后续 Redis 日志修复修改范围由 625-test server suite 覆盖。Lumilake 为 **758 passed, 1 skipped**。
- **Completed / SoC**：`07-flowmesh-check.sbatch` 与 `09-lumilake-dryrun.sbatch` 已在 job `770489` 验证真实退出码并部署到 `~/slurm/`。全栈 job `771016` 的 `TimeLimit=01:00:00`，实际用时 `00:03:09`，Slurm 结果 `COMPLETED 0:0`；它在 restore 前后各完成一轮 L2 **19/19** 和 `E2E_SMOKE_OK`，完成 quiesced checkpoint/validate/同 allocation restore，并以 `original_exit_code=0` / `cleanup_exit_code=0` 完成 final checkpoint 与精确进程回收。
- **有效失败证据**：job `770815` 因 SoC Redis `5.0.3` 不支持 ACL 配置而失败，但 cleanup 正确保留原退出码（`original=1`, `cleanup=0`）；修复为 Redis 6.2+ named ACL，老版本随机 `requirepass`，并新增“未认证 PING 必须被拒绝”探针。job `770992` 又暴露 Redis 5 会重写自身 `/proc/.../environ` 标记；它同样保持 `original=1`, `cleanup=0`，随后以 tracked supervised wrapper 保留稳定进程身份。
- **Completed / 精确版本复验**：job `771030` 为 `COMPLETED 0:0`，`TimeLimit=01:00:00`，用时 `00:02:43`，stderr 0 bytes，cleanup `0/0`；全栈 marker 和 GPU UUID 证据见第 10、13、18 节。
- **Completed / 最终签收**：job `771041`（analytics `673a053`）为 `COMPLETED 0:0`，`Elapsed=00:03:22`，`TimeLimit=01:00:00`，节点 `xgpg0`；stdout/stderr mode `0600`，stderr 0 bytes，两轮 stack-up/restore、6 次 L2 19/19、2 次 E2E、2 次 checkpoint commit、2 次 restore complete、cleanup `0/0`。日志与 archive 的 secret assignment/raw credential URL/bearer 扫描命中数都为 0；当前 active stack jobs 为 0，部署内容 checksum dry-run 无差异。
- **Residual**：真实 Slurm `TERM`/timeout 尚未做独立故障注入；supervisor 异常 crash 后 reservation quarantine 仍不持久；非独占节点、跨节点 mTLS/网络策略、Redis in-flight 状态恢复、periodic checkpoint 跨存储一致性、VastAI 预算/egress 与粗粒度 admin bearer 仍需接受或继续治理。

## 2. 审计范围与证据

### 2.1 已审阅材料

- 项目记忆与阶段记录：`memory/INDEX.md`、`memory/phase-0-onboarding.md`、`memory/phase-1-wk1-2-lakehouse-baseline.md` 及后续 phase 文件。
- 项目与部署文档：`docs/phase-1-report.md`、`cluster/SoC-cluster-deploy-plan.md`、`cluster/SoC-admin-request.md`。
- 研究/审计材料：`research-notes/`、`audit/` 中的架构、workflow 与实现差异记录。
- 当前部署脚本：`cluster/scripts/stack/`、`cluster/scripts/07-flowmesh-check.sbatch`、`cluster/scripts/09-lumilake-dryrun.sbatch`，以及 Git source-of-truth `analytics-agent/deploy/soc/`。
- 三个代码仓库的 committed delta：`FlowMesh/`、`Lumilake/`、`analytics-agent/`；部署以 commit/hash 而非未提交 working tree 定义。
- 历史与本轮 Slurm 证据：Phase 1 job `740372`，历史检查 jobs `740343`、`740352`、`740358`、`740359`、`740360`，本轮检查 jobs `770484`、`770489`，全栈失败发现 jobs `770815`、`770992`，全栈 restore drills `771016`、`771030`，以及最终 log-mode/全栈签收 job `771041`。

### 2.2 未包含或受限部分

- 本报告不是 SoC 集群管理员级别的主机、内核、Slurm cgroup 或网络 ACL 审计。
- 未假定其他普通用户能读取该账户 `$HOME`；审计时已知 `$HOME` 权限为 `0700`。这降低了日志泄漏的即时可读性，但不撤销已经写盘的 secret，也不覆盖账户失陷、管理员、备份、误复制或将来权限变化。
- “本地测试通过”不等于已部署；jobs `770815`/`770992` 已证明 partial-startup 失败 cleanup，jobs `771016`/`771030`/`771041` 已证明 normal EXIT、restore 前后 health/E2E 和 GPU allocation 不变量，但仍不等于已完成真实 Slurm `TERM`/timeout 或 supervisor-crash 故障注入。
- SoC 上任何新增验证 job 必须保持 **单个 job 最长不超过 1 小时**；本文不把旧的 3-day 生产 allocation 当成本轮安全验证。

## 3. 威胁模型与信任边界

### 3.1 需要保护的资产

- PostgreSQL 中的私有数据、工作流输入、结果与元数据。
- MinIO `lumilake-private`、`lumilake-public` 和 archive artifacts。
- FlowMesh worker 执行权限、GPU allocation、模型缓存、API keys 与结果目录。
- Slurm 作业用户的 NFS home、`~/src`、`~/lakehouse-checkpoints` 和 SSH 凭据可达范围。
- VastAI/cloud provider 的账户余额、远端实例和经 worker 传递的数据/凭据。

### 3.2 对手与故障场景

- 与本作业共用同一 compute node 的其他 Slurm 用户或被攻陷进程。
- 能从集群内部网络访问 FlowMesh 端口的客户端。
- 获得固定 token、日志副本或仓库开发凭据的调用者。
- 已认证但只应提交 workflow、却不应管理宿主 worker 的普通调用者。
- 恶意/被替换的 OCI 镜像或 VastAI 镜像。
- Slurm `TERM`/`KILL` timeout、节点故障、tmux/service crash、部分启动和重复启动。
- Slurm 提供 GPU index、GPU UUID 或 MIG UUID 等不同 `CUDA_VISIBLE_DEVICES` 形式。

### 3.3 关键边界

当前 Phase 1 拓扑是同一 Slurm allocation 内的单节点栈：

- `Lumilake -> FlowMesh HTTP -> FlowMesh gRPC/supervisor -> native workers`
- `Lumilake/worker -> lumid-gateway -> PostgreSQL + MinIO`
`FlowMesh -> Redis (control + telemetry)`

`127.0.0.1` 只限制“别的主机”直接连接，并不在同一 Linux network namespace 内区分 Slurm 用户。共享节点上的其他作业仍可访问该节点的 loopback 端口。因此 loopback 是缩小攻击面的方法，不是共享 HPC 上的认证替代品。

## 4. Findings 总表

| ID | 严重度 | 问题 | 当前状态 |
|---|---:|---|---|
| FM-01 | Critical | native provider 接受任意 `command/cwd`，可形成宿主机代码执行 | 代码/测试/GitHub/SoC Completed；native 无 sandbox residual |
| FM-02 | Critical | FlowMesh 默认无认证；部署 bind 变量不生效，HTTP/gRPC 可能监听所有网卡 | API key、loopback、TLS/worker token 已在 SoC L2 验证；跨节点 mTLS residual |
| FM-03 | Critical/High | Docker provider 可被请求覆写镜像来源/版本、host path、container identity；SSH 模式挂载 Docker socket | request allowlist/test/GitHub Completed；operator socket residual |
| FM-04 | High | VastAI 可被请求选择镜像/instance/credential 并消耗远端资源 | privileged field allowlist 代码/测试/GitHub Completed；预算/egress residual |
| DATA-01 | Critical/High | PostgreSQL loopback `trust` + bootstrap superuser；Redis 无认证；固定 MinIO/gateway 凭据 | SCRAM、随机 secret、least privilege、Redis 5 `requirepass`/6.2+ ACL 已在 job `771016` 验证 |
| SEC-01 | High | `set -x` 在 source secrets 前开启，secret 进入 Slurm stderr；Redis URL 可从 lifecycle/failure log 泄漏 | 新栈及 FlowMesh sync/async Redis 日志已修；历史日志继续受限管理 |
| REL-01 | High | checkpoint 非原子、只备份一个 bucket，且完全没有 restore | atomic create/validate/restore 及同 allocation drill 在 job `771016` Completed；consistency/retention residual |
| REL-02 | High | Slurm timeout/TERM 清理可能重复、继续循环或在 KILL 前未完成 | 正常 EXIT 与启动失败 cleanup 已实机验证；真实 TERM/timeout 注入 Residual |
| OBS-01 | High | `/healthz` 静态 OK，不验证关键依赖或 worker | 分层 health 已部署；job `771016` restore 前后均 L2 19/19 |
| DATA-02 | Medium | seed 表缺唯一约束，重复 stack-up 会重复写入 | transaction/unique/upsert 已在 restore 后再次 stack-up 验证 |
| OPS-01 | High | `stack-down.sh` 使用宽泛 `pkill -f` | PID/PGID/deployment ownership 已实机回收；Redis 5 wrapper 和 `/proc` race 已修 |
| GPU-01 | High | GPU allocation token 被当作 host integer；worker 可能上报整机 GPU | opaque-token/logical-slot/subset 已在 job `771016` GPU L2 实机验证 |
| GPU-02 | High | worker stop 未确认仍释放 reservation/移除 registry | 受控 stop 语义与测试 Completed；crash-persistence residual |
| TEST-01 | Medium | `07-flowmesh-check.sbatch` Python heredoc 控制流损坏 | Completed |
| TEST-02 | High | `09-lumilake-dryrun.sbatch` pipeline 掩盖真实退出码，历史结果有假成功/假失败 | Completed |
| SLURM-01 | Medium/High | stack job 未申请独占节点，所有 loopback 服务与其他作业共处网络 namespace | Residual/待决策 |
| SCM-01 | Medium | 根目录 `cluster/` 不在 Git repo，集群已部署脚本没有可靠 source-of-truth | `analytics-agent/deploy/soc/` 已建立 Git source-of-truth，已部署/验证 revision `673a053` / PR #1 |

## 5. FlowMesh 执行面

### 5.1 FM-01：native provider 的任意宿主机代码执行

**原问题与来源**

这不是原来的 Docker/VastAI provider 直接具有的 `command/cwd` 字段。它来自为 SoC 无容器环境新增的 native provider patch：`NativeWorkerConfig` 暴露 `command: list[str]`、`cwd: str` 和 `log_dir: str`，`NativeWorkerAdapter._start()` 将 `command` 直接交给 `subprocess.Popen(..., cwd=...)`。

原有 Docker provider 没有相同的 `command/cwd` 字段；它的危险路径是可控镜像和 Docker socket，见 FM-03。VastAI 的主要直接影响面在远端实例，见 FM-04。

**可利用链**

1. FlowMesh 没有 IdentityProvider plugin 时，原生行为是把任何调用者映射为默认 `admin`。
2. `POST /stack/workers` 接受 `WorkerInitConfig.worker_config` 并把它发送给本地 supervisor。
3. 请求指定 `provider: native`、任意 `command`/`cwd`。
4. supervisor 以 Slurm 作业账户直接执行该 argv。虽然没有 `shell=True`，但“任意 argv 的第一个可执行文件”本身就是代码执行，不需要 shell injection。
5. 进程继承 supervisor 的环境和该 Unix 账户可访问的文件，影响可扩展到 NFS home、数据、token 和同账户作业。

**修复设计**

- 从 API schema 删除 `command`、`cwd`、`log_dir`，并设置 `extra="forbid"`，让旧 payload 明确失败而不是静默忽略。
- 只执行固定 argv `[sys.executable, "-m", "worker.main"]`，cwd 固定到 FlowMesh source root。
- log 目录固定到 supervisor 管理的 `WORKER_HB_DIR/native-logs`，权限 `0700`；文件名由 token digest 生成，不接受 alias/path。
- 配合 FM-02：生产栈必须设置强随机 bearer，不能只依靠参数收紧。
- provider 管理权限应与普通 workflow submit 权限分离；当前静态 bearer 仍是粗粒度 admin。

**实际修改文件（代码、测试与 GitHub Completed；SoC 行为已验证）**

- `FlowMesh/src/server/supervisor/adapters/native.py`
- `FlowMesh/src/server/supervisor/manager.py`（native 动态 request allowlist）
- `FlowMesh/tests/server/test_native_worker_provider.py`
- `FlowMesh/tests/server/test_worker_manager_gpu.py`
- `FlowMesh/docs/ENV.md`、`FlowMesh/docs/ARCHITECTURE.md`

**当前证据**

- 主修复 commit `676c9aca8ba98a3f5d25ba8d3355a921a2662ef6` 已删除 `command/cwd/log_dir`，拒绝 extra/自定义 heartbeat，固定 argv/cwd；runtime/log/heartbeat 路径在 operator `WORKER_HB_DIR` 下，名称由 token digest 生成，并给固定 `subprocess` 输入写明 Bandit B603 理由。当前分支 HEAD 是 `0b8be48e711515e566a5125ff56b67166a997473`。
- 新测试覆盖 legacy `command/cwd/log_dir/hb_file` rejection、固定 argv/cwd、runtime path 与 request allowlist。
- 主 hardening commit 的全套验证为 **702 passed**，并通过 Ruff、Black、isort、targeted mypy、Bandit、gitleaks、codespell、env sync 与 diff check。在两个 Redis 日志 follow-up 后，当前 server suite 为 **625 passed**；这是当前 HEAD 的直接回归证据，不把之误写成再次全套 702。分支已 push，PR：[ff98li/FlowMesh#1](https://github.com/ff98li/FlowMesh/pull/1)。
- jobs `771016` 与 `771030` 都在 SoC 启动固定 native workers，restore 前后通过 worker freshness/gRPC/GPU-subset L2 探针。其中 `771030` 是 `0b8be48e...` 精确部署版本复验，最终 `COMPLETED 0:0`。

**剩余风险**

- 官方 worker 本来就是执行引擎；获得 FlowMesh admin bearer 的主体仍可提交具有高执行能力的任务。参数收紧不能替代身份、授权和任务 sandbox。
- native worker 与 supervisor 同 Unix 用户、同主机文件系统，没有容器边界；模型 `trust_remote_code`、executor/plugin 等入口仍需独立治理。

### 5.2 FM-02：默认无认证与实际 bind 配置错误

**根因**

- `server/auth/security.py` 的既有语义是：没有注册 IdentityProvider 时，任何 token（包括空 token）都得到默认 admin principal。
- stack env 设置 `SERVER_BIND_HOST=127.0.0.1`，但 FlowMesh HTTP 配置实际读取 `SERVER_APP_HOST`；原来的 gRPC 配置也默认 `0.0.0.0`。因此文档/脚本以为是 loopback，实际监听范围可能更大。
- `LUMILAKE_RUNTIME_TOKEN` 原脚本为空，FlowMesh 没有强制 bearer。

**影响**

它把 FM-01 从“受信管理员可误用的危险配置”升级为集群内部可达的未认证宿主机执行入口。即使 FM-01 修掉，未认证管理 API 仍可创建/停止 worker、提交执行任务或读取控制面信息。

**修复设计**

- 添加显式 opt-in、fail-closed 的 `FLOWMESH_REQUIRE_API_KEY=true`；要求非空 `FLOWMESH_API_KEY`，使用 constant-time compare，并让 Lumilake/runtime/worker 都使用同一轮部署生成的 bearer。
- stack 明确设置 `SERVER_APP_HOST=127.0.0.1` 与 `SERVER_GRPC_HOST=127.0.0.1`；不要继续设置 FlowMesh 不读取的 `SERVER_BIND_HOST`。
- 为 supervisor/worker gRPC 每个 deployment 生成短期私有 CA/server certificate；worker 以 CA 验证 server，RPC 再使用随机 worker token 认证。禁用 Phase 1 不需要的 port-forward/SSH/serve proxy。
- HTTP health/readiness 的认证策略要明确：`/livez` 可不认证；管理 API 和能暴露拓扑的 endpoint 必须认证。
- 多节点模式不能继续使用 loopback；应采用受限 routable address + firewall/ACL + TLS，gRPC 优先 mTLS。

**实际修改文件（代码、测试与 GitHub Completed；SoC 行为已验证）**

- `FlowMesh/src/server/auth/security.py`
- `FlowMesh/src/server/env.py`
- `FlowMesh/src/server/config.py`
- `FlowMesh/tests/server/test_hooks_wiring.py`
- `FlowMesh/tests/server/test_config.py`
- `FlowMesh/docs/API.md`、`FlowMesh/docs/ENV.md`
- `FlowMesh/cli/stack/src/flowmesh_cli_stack/{env_schema.py,assets/.env.example}`
- `cluster/scripts/stack/stack-env.sh` 已接 `SERVER_APP_HOST`、`SERVER_GRPC_HOST`、API keys、gRPC TLS 与 proxy-disable，并已镜像至 `analytics-agent/deploy/soc/stack/stack-env.sh`。job `771016` 的 L1/L2 证明未认证 FlowMesh/Lumilake worker API 被拒绝、带 bearer 请求成功，且 worker 已经通过 TLS + token gRPC 完成注册/heartbeat。

**剩余风险**

- 计划中的静态 bearer 是单租户 Phase 1 的最低可行修复，不是多租户 RBAC。
- 本地 stack 已从“仅 loopback”提升为 TLS server authentication + 随机 worker token RPC authentication。它适合当前单 allocation 模型；若扩展到跨节点/多租户，mTLS 与每个 node/worker 的可撤销 identity 仍优于共享/临时 token。

### 5.3 FM-03：Docker provider 的镜像与 Docker socket 控制

**与 native 的区别**

旧 Docker provider 没有直接 `command/cwd` 字段，但动态请求的 `worker_config` 与管理员 default config 合并后，可覆写：

- `docker_registry` 与 `version`：决定 `get_worker_image_name()` 最终启动哪个镜像；
- `enable_ssh`：为 true 时，worker container 会挂载 `/var/run/docker.sock` 并加入 socket group；
- 其他 host-sensitive 字段：`results_dir`、`hf_cache_dir` 会成为宿主 bind mount，`container_name` 可能碰撞已有 stopped container。

Docker daemon 通常拥有 root 等价的宿主机控制能力。攻击者指定恶意镜像后，即使普通 worker 没有 socket，也可读取被挂载的 host path；若再启用 SSH/socket，容器可直接调用 Docker API 创建 privileged container 或挂载宿主 `/`。所以它与 native 的差别是路径更间接，不代表安全。

**修复设计**

- 动态 API 使用 fail-closed allowlist，而不是只拉黑少数字段。建议只允许 `worker_type`、受上限约束的 `gpu_count/cuda_devices`、alias/tags 等低风险字段。
- 镜像 registry、digest（优先 digest 而不是 mutable tag）、host mount source、container identity、SSH/socket 和 secrets 必须来自管理员只读配置。
- `enable_ssh` 默认 false；不要把 Docker socket 挂进通用 worker。若产品确实需要 nested container/session，使用单独 broker、socket proxy 与最小 API allowlist。
- 可选再加 registry allowlist、签名验证/SBOM、镜像拉取策略和 egress 限制。

**当前实现（代码、测试与 GitHub Completed；Docker 主机实机 residual）**

- `FlowMesh/src/server/supervisor/manager.py` 已改为 fail-closed allowlist。Docker 动态 request 只允许 resource selection/metadata：`worker_type`、`gpu_count`、`cuda_devices`、`worker_alias`、`tags`、`network_bandwidth`、`worker_cost_per_hour`。
- `docker_registry`、`version`、`enable_ssh/ssh`、`results_dir`、`hf_cache_dir`、`container_name`、callback target、credentials 等都必须来自 operator-owned default config。
- 只有管理员显式设置 `FLOWMESH_ALLOW_PRIVILEGED_WORKER_OVERRIDES=true` 才绕过；stack 明确把它设为 false。
- `FlowMesh/tests/server/test_worker_manager_gpu.py` 已加入 privileged field rejection 与 safe field acceptance tests；`docs/ENV.md` 记录边界。

SoC 当前没有 Docker/socket，所以此项不是本次在线栈最直接的入口，但会影响 fork 在其他 Docker 主机上的安全性。上述 702-test suite 与安全/静态检查已通过并 push；生产仍应使用 image digest/signature，而不是只靠 tag。

### 5.4 FM-04：VastAI 边界

VastAI provider 同样允许配置 `docker_registry`/`version`，最终将镜像交给远端 instance。它通常不会直接获得 SoC 宿主机 Docker daemon，因此与 native/Docker-host 风险不同；主要影响是：

- 在远端执行攻击者镜像；
- 消耗云账户余额、创建/销毁实例；
- 读取下发给远端 worker 的 token、数据或模型凭据并外传；
- 通过 FlowMesh 网络连接反向影响控制面。

**本地实现**：VastAI 动态 request 也改为 allowlist，只允许 offer-search/resource metadata（如 `specs`、`disk`、`order`、`search_limit`、`label`）；禁止请求选择 `docker_registry/version`、已有 `instance_id`、supervisor target、host path 或 credentials。`VastAIWorkerAdapter.stop()` 未确认远端 shutdown 时也不再释放 offer reservation。

**状态**：代码级 privileged override、stop confirmation 与测试/commit/push 已完成；云治理仍是 Residual。仍应使用 trusted image digest、管理员专用 provider、实例规格/并发/预算/region 限制、最小 worker secret/data 权限和 egress/lifecycle 审计。

## 6. DATA-01：shared-node loopback、`trust` 与固定凭据

### 6.1 为什么当前组合危险

| 服务 | 原始配置 | 共享节点影响 |
|---|---|---|
| PostgreSQL | `initdb --auth=trust`；gateway 使用 initdb 创建的 `lumilake` bootstrap role；无 password DSN | 同节点进程可声明自己是 `lumilake` 而无需密码。bootstrap role 是数据库 superuser，可读改全部数据/role/config；PostgreSQL superuser 还可能借助 server-side 功能扩大到运行 postgres 的 Unix 账户。 |
| Redis | `--bind 127.0.0.1`，无 `requirepass`/ACL，control 与 telemetry 共用同实例 | 同节点进程可读写控制面 key、伪造状态、清空队列或造成拒绝服务。loopback 不隔离 Slurm 用户。 |
| MinIO | 固定 root user/password `lumilake/lumilake_password` | 凭据公开在脚本；拿到后可读取 private/archive、写对象、改 bucket policy。root credential 权限过大。 |
| lumid-gateway | 固定 bearer `devtoken`；使用 MinIO root credential | 知道 repo 或 xtrace 日志内容的调用者可通过 gateway 读写数据；gateway compromise 继承 root S3 权限。 |
| FlowMesh | 空 runtime token；默认无认证 | 与 FM-01/FM-02 组合为执行面接管。 |

固定值本身也使“轮换”不可操作：所有历史 deployment 共用相同 secret，仓库读者、日志读者和旧节点都持有同一权限。

### 6.2 这只在集群成立吗

不是。风险由“谁能到达端口”和“是否共享主机/凭据”决定：

- **个人独占笔记本**：loopback 把远程攻击面显著缩小，但本机其他进程、浏览器中的 SSRF、恶意依赖和账户失陷仍可连接；固定 secret 仍不应提交。
- **当前共享 Slurm 节点**：不同用户作业共享主机 network namespace，loopback 不是用户隔离，风险最直接。
- **多个异地但每节点自包含的单节点栈**：每个节点的 `127.0.0.1` 互不连通，但每台共享主机内的问题照旧；复用固定 secret 会把一个泄漏扩散到所有地点。
- **真正跨节点的分布式栈**：服务必须监听非 loopback 地址，攻击面从“同机”扩展到可路由网络；此时 PostgreSQL `trust`、无认证 Redis、明文 bearer 的风险更高，必须同时加入网络 allowlist 和 TLS/mTLS。

### 6.3 修复目标

- 每次 deployment 首次启动生成独立随机 secret，写入 `0600` secret env/file；已存在时复用，不能每次进程重启随意改变。
- PostgreSQL：`scram-sha-256` host auth；bootstrap admin 与 gateway runtime role 分离。gateway role 必须 `NOSUPERUSER NOCREATEDB NOCREATEROLE`，只获目标 database/schema/table 的必要权限。
- Redis：ACL user 或至少强随机 password；URL 使用认证；禁用/重命名高危命令不是认证替代品。
- MinIO：root credential 仅做 bootstrap；为 gateway 创建最小权限 service account/policy，并分别限制 private/public/archive 操作。
- FlowMesh：随机 API key + `FLOWMESH_REQUIRE_API_KEY=true`，Lumilake/runtime/worker 接线；gRPC 另行认证。
- 所有 probes、CLI 和 restore 命令同步使用新认证，响应与日志不能包含 secret/DSN。

### 6.4 改动规模

**当前单节点 Phase 1**：属于中等规模部署改造，不需要改变 Lumilake/FlowMesh 调度算法，也通常不需要业务 schema migration。主要涉及：

- `stack-env.sh` 的 secret file/generation、认证 URL 与正确 bind 变量；
- `stack-up.sh` 的 PostgreSQL HBA/roles、Redis ACL/requirepass、MinIO service account/bootstrap；
- `health.sh`、checkpoint/restore、CLI/sbatch 对新凭据的使用；
- FlowMesh 的 opt-in bearer 支持与 gateway/Lumilake readiness；
- secrets 轮换及旧 stderr 处理。

**跨节点部署**：改动明显更大，还需地址/服务发现、Slurm placement、firewall/ACL、TLS certificate 生命周期、gRPC mTLS、每节点/每服务 identity、跨站 secret distribution 和 failure/retry 设计。不能简单把 `127.0.0.1` 改成 `0.0.0.0`。

**本地实现状态**

- `stack-env.sh` 以 `umask 077` 在 allocation-scoped `/tmp/lumilake-stack-<job>/run/secrets.env` 原子生成随机 PostgreSQL admin/runtime、Redis、MinIO root/app、gateway 与 control-plane secrets；验证 owner/mode `0600`，相同 deployment 重载复用、不同 deployment 不复用。
- PostgreSQL 改为 local/host `scram-sha-256`；bootstrap `lumilake_admin` 与 `NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION` runtime `lumilake` 分离，gateway/health 只用 runtime role。
- Redis 6.2+ 关闭 default user 并启用随机 named ACL；6.0/6.1 缺少所需 Pub/Sub channel ACL，SoC 的 Redis 5.0.3 更不支持 ACL，因此旧版本自动使用随机 `requirepass`。两种模式都会同步生成带认证的 FlowMesh URL/CLI/health 配置，绝不回退到无密码。
- Lumilake 会把调用者 bearer 转发给 FlowMesh，因此静态单租户模式下两者共享同一个随机 control-plane bearer；gateway 使用另一个独立随机 bearer。curl bearer 放在 `0600` config 中，避免出现在 argv。
- FlowMesh HTTP/gRPC loopback bind、gRPC TLS/worker token、无关 proxy disable 已接线。

MinIO root credential 现在只用于 bootstrap。脚本创建独立 app identity，并把 policy 限制到 private/public 两个 bucket 所需的 list/get/put/delete/multipart 操作；gateway、health、checkpoint 与 restore 使用 app identity。root 与 app credential 每个 deployment 都随机且不同。

实机推进过程同时验证了“认证不能只看脚本外观”：

- job `770815` 在 Redis `5.0.3` 读取 `user default off` 时立即失败；这证明不能把 Redis 6 ACL 语法不加判断地发给集群老版本。脚本改为：只在 server 版本至少 6.2 且配对 `redis-cli` 支持 `--user` 时使用 named ACL，其他版本使用每 deployment 随机 `requirepass`，不存在无密码 fallback。
- `health.sh` 不只执行带凭据 `PING`/TTL roundtrip，还以空白环境发出未认证 `PING`，要求结果为 `NOAUTH`/`WRONGPASS`；ACL 模式还要求 `ACL WHOAMI` 等于指定 user。这避免了“带密码客户端可用，但 server 其实也接受无认证”的假成功。
- job `771016` 两轮 L2 19/19 都通过 PostgreSQL SCRAM/no-trust/non-superuser、Redis authenticated roundtrip + unauthenticated rejection、MinIO private/public policy、gateway/FlowMesh/Lumilake bearer 正反路径。新 allocation 的随机凭据已替代旧固定开发值；gateway 使用 PostgreSQL 非 superuser runtime role 与 MinIO bucket-scoped app identity。

因此 DATA-01 对当前单节点栈已有部署与成功/失败路径证据。历史 stderr 仍应作为受限敏感资料管理；未来跨节点时仍需 TLS/mTLS、网络 allowlist 与可撤销 service identity。

## 7. SEC-01：`set -x` 泄漏 secret

原 `slurm-stack.sbatch` 在 `source "$STACK_DIR/stack-env.sh"` 前执行 `set -x`。Bash xtrace 会把展开后的赋值和命令写入 stderr，因此 `STACK_TOKEN`、MinIO password、带凭据的 DSN/Redis URL 等会出现在 `08-lumilake-stack-%j.err`。

`$HOME=0700` 意味着其他普通用户目前通常无法 traverse 到这些日志，这是一层现实缓解；但 secret 已经落盘，仍可能通过以下路径泄漏：账户被盗、管理员/备份、把 log 复制进报告/Git、以后放宽权限、调试时共享。因此应按“已披露给持有日志的主体”处理，而不是认为没有问题。

**修复**

- 使用 `set -Eeuo pipefail`，不要全局启用 xtrace。
- 如果必须短时 debug，只在不含 secret 的命令周围 `set -x`/`set +x`，并确保 source/认证 CLI/URL 不在 xtrace 区间。
- secret 文件 `umask 077` + mode `0600`；日志只记录 secret 是否配置、长度/指纹的安全摘要，不记录值。
- 在部署修复后轮换 PostgreSQL、Redis、MinIO、gateway、FlowMesh token；受影响旧 stderr 应删除、受限归档或先脱敏，不能进入 Git。

**stack 状态**：新 `slurm-stack.sbatch`、`05d-lumilake-groups.sbatch` 与 `09-lumilake-dryrun.sbatch` 已在 source secret 前移除 xtrace；offline regression 扫描 `set -x` 与已知固定值。新 allocation 会生成全新 secret，相当于 deployment rotation。job `771016` 的唯一 stderr 是后述 `/proc` `awk` race，没有 xtrace/secret 输出；该 race 已在 analytics commit `083f448` 修复。历史 stderr 仍应删除、受限归档或先脱敏；只删除 xtrace 不能让旧日志中的值自行消失。

analytics commit `673a0531fa55dbc7e9cd809a0fa002f84c0c60de` 进一步修复了 Slurm 默认创建 `.out/.err` 时可能继承过宽 mode 的问题：`05d`、`07`、`08` 全栈和 `09` jobs 均在启动后立即 `umask 077` 并对当前 `${SLURM_JOB_ID}` 的 stdout/stderr 精确 `chmod 600`。集群上历史项目 logs 也已收紧，当前 group/world-readable 计数为 **0**。最终 job `771041` 的 stdout/stderr 均为 `0600`，stderr 为 0 bytes；其新 logs 与 archive 对 secret assignment、raw credential URL 与 bearer 的扫描命中数均为 0。这个权限修复降低同账户外的直接可读性，但不取代对历史已泄漏凭据的轮换。

**FlowMesh Redis URL/日志/异常链脱敏**

后续审阅又发现，即使 shell 完全没有 xtrace，FlowMesh Redis client 也可能主动把密码写入日志：原 sync/async `_connect()` 失败路径使用原始 `url` 和 `%s` exception text，连接库 exception 又可能嵌入整个 DSN；`raise SystemExit(1) from exc` 还会把含凭据的 exception 保留为 cause/context，以后的 crash reporter 可再次输出。`flush_all()` 的 lifecycle info log 也直接使用 `self.control_url`/`self.telemetry_url`。

修复分两个 signed commits：

- `3c89ec6471e41e1e391e5fe53762c64d1665fe91`：sync/async 连接失败只记录 `_redact_url(url)` 和 exception type，不记录不可信 exception text。
- `0b8be48e711515e566a5125ff56b67166a997473`：不用 `raise ... from exc`，并在离开 exception handler 后抛出 `SystemExit`，证明 `__context__ is None`；sync/async `flush_all()` 也统一脱敏 URL。

对应文件为 `FlowMesh/src/server/clients/redis.py` 和 `FlowMesh/tests/server/test_redis_connection_logging.py`。测试显式用“exception text 自带 credential-bearing URL”的失败 fixture，覆盖 sync constructor、sync ping、async constructor 以及 sync/async flush logs，同时断言 raw URL/password 不在 log 中。analytics-agent 以下面两个 vendor patches 完整保存该追踪关系：

- `analytics-agent/vendor/flowmesh/0003-fix-redact-redis-connection-failures.patch`
- `analytics-agent/vendor/flowmesh/0004-fix-redact-redis-lifecycle-logs.patch`

当前 FlowMesh HEAD server suite **625 passed**；主 hardening 全套 **702 passed** 仍作为较早基线，不混同两者的证据范围。

## 8. REL-01：checkpoint 声称可恢复，但代码没有 restore

### 8.1 原始行为

- `slurm-stack.sbatch::checkpoint()` 直接把 `pg_dump` 重定向到最终文件名；命令失败也被 `|| true` 吞掉，可能留下空/截断文件。
- 只 mirror private bucket，未保存 public bucket；archive 是否位于 private prefix 依赖配置，缺少显式 manifest。
- 没有 checksum、完成标记、versioned checkpoint directory 或原子 `latest` 指针。
- `stack-up.sh` 只在 `/tmp` 初始化 PostgreSQL/MinIO 并执行 seed，完全没有查找/校验/恢复 NFS checkpoint 的逻辑。
- Redis 配置为无 RDB/AOF；控制面/in-flight job 不在 checkpoint 范围内。

因此旧文档中的“`/tmp` data rehydrates from checkpoints + seed”不符合代码事实。实际重启后最多重新 seed demo DB；历史 PostgreSQL/MinIO 状态不会自动回来。

### 8.2 正确恢复模型

建议每次 checkpoint 建立 versioned staging 目录，例如：

1. 在 `checkpoint.tmp.<id>/` 生成 PostgreSQL dump，并 mirror private、public（以及明确的 archive）bucket。
2. 每个 artifact 成功后计算 checksum/size，写 manifest（版本、时间、job/node、schema、bucket 列表、工具版本）。
3. 全部成功才写 `COMPLETE`，将 staging 原子 rename 为不可变 version 目录，再原子更新 `latest` symlink/file。
4. 保留最近 N 份，清理必须在新 checkpoint 完成之后；绝不使用 `mc mirror --remove` 破坏唯一好副本。
5. stack-up 在新 `/tmp` 或明确 `--restore` 时：锁定 -> 找到最新 COMPLETE -> 校验 manifest/checksum -> 初始化服务 -> 恢复 DB/buckets -> 做数据一致性检查 -> 再执行幂等 seed/migration。
6. restore 失败应 fail closed 或显式选择“fresh stack”，不能悄悄启动半恢复状态。

Redis 应明确记录为 **不恢复 in-flight scheduler/control state**。若未来要求 job 续跑，需要设计 Redis AOF/RDB、FlowMesh/Lumilake reconciliation 和幂等执行，而不是简单复制 Redis 文件。

**实际修改文件与实机证据**

- `cluster/scripts/stack/checkpoint.sh`：custom-format `pg_dump`、`pg_restore --list` validation、private/public bucket mirror、manifest、每个文件 SHA-256、generation staging、同 filesystem atomic rename 与 atomic `latest`。
- `cluster/scripts/stack/stack-up.sh`：在 Redis/gateway/FlowMesh/Lumilake 启动前查找并校验 latest；无 checkpoint 返回明确 fresh 状态，损坏 latest 则 fail closed；恢复 DB/两个 bucket 后再执行 transactional idempotent seed/grants。
- `cluster/scripts/stack/slurm-stack.sbatch`：periodic/final create、validate 与失败时保留 previous last-good。
- manifest 明确 `redis_restore_policy=discard_inflight`，不承诺在途作业恢复。

job `771016` 在单个 `TimeLimit=01:00:00` allocation 内执行了真实 rehearsal：先运行 L2/E2E，精确停止 writers，create + validate quiesced checkpoint，再停止 PostgreSQL/MinIO/Redis，移除当次 ephemeral ready markers，从 latest generation 恢复 PostgreSQL 与两个 MinIO bucket，重新启动整栈并再次运行 L2 19/19 和 `E2E_SMOKE_OK`。正常退出时还成功提交 final checkpoint，cleanup summary 为 `original_exit_code=0` / `cleanup_exit_code=0`。

这证明 create/validate/restore/restart 的真实程序路径可用，且 restore 后再 seed 没有因唯一约束冲突失败。证据边界也要保持清楚：此 job 没有额外注入一组“非 seed DB sentinel + 两 bucket 预存 sentinel objects”并逐一比对其内容 checksum；若要把数据完整性证据提升到这一级，仍可补一个 <=1h 定向 drill。这不改变“原代码完全没有 restore”已被修复的结论。

**剩余设计点**

- 当前 periodic checkpoint 在 writers 活跃时顺序执行 PostgreSQL consistent dump 与两个 S3 mirror，不是三种存储的同一原子 consistency cut；quiesced final checkpoint 更强。应短暂停 admission/quiesce，或在 manifest 明确 periodic 为 `best_effort` 并只把 quiesced generation 当 authoritative recovery point。
- generation 尚无保留上限；3-day allocation 每小时生成会持续增长 NFS。需设置安全 retention，永不删除 `latest` 和最后一份已验证好副本。
- PostgreSQL + 两 bucket 的 restore 无法成为一个跨系统 transaction；中途失败会 fail closed、下次重试，但仍要在 runbook 记录 partial restore 行为。

## 9. REL-02：Slurm timeout 与 cleanup

原脚本使用 `trap cleanup EXIT TERM INT`，但没有一次性 guard、没有明确 signal exit code，并且主循环本身没有严格错误模式。Bash 捕获 `TERM` 后执行 trap 并不自动保证整个脚本立即、只执行一次退出；后续 `EXIT` 还可能再次调用 cleanup。Slurm timeout 通常先 TERM、宽限期后 KILL，重复/过慢的 dump + mirror + shutdown 可能在最终 KILL 前未完成。

**修复设计**

- 顶层使用 `set -Eeuo pipefail`。
- `TERM`/`INT` handler 只记录目标退出码并调用一个带 guard/lock 的 cleanup；cleanup 完成后明确 `exit 143/130`。
- `EXIT` trap 负责兜底，但通过 `cleanup_started`/lock 保证 exactly-once。
- cleanup 顺序：停止接受新任务 -> 做有明确 deadline 的 checkpoint -> 优雅停止 child process groups -> 必要时升级 TERM/KILL -> 保存有限日志 -> 返回原始/信号 exit status。
- checkpoint 和 service stop 各自设置 timeout；不能用无限网络 mirror 吃完 Slurm grace。
- `stack-up` 失败的 partial state 也走相同清理路径。

**实现**：`#SBATCH --signal=B:TERM@600` 在 walltime 前 10 分钟通知；TERM/INT handler 保留 143/130，EXIT cleanup 有 one-shot guard，先精确停止 writers、做 bounded final checkpoint、再停 stores/归档日志，cleanup failure 不再被吞掉。`STACK_TEST_MODE=1` 让 <=1h job 启动、L2/E2E/checkpoint/restore 后正常退出并测试同一 cleanup。

**实机证据**：

- job `770815` 启动时因 Redis 5 ACL 不兼容退出 1；cleanup summary 为 `original_exit_code=1`, `cleanup_exit_code=0`，证明 partial startup 失败不会被 cleanup 伪造成成功。
- job `770992` 因 Redis 5 改写 process title/擦除 `/proc/$pid/environ` 中 deployment marker 而发生 ownership check 失败；cleanup 同样是 `original=1`, `cleanup=0`。修复不是放宽 ownership 验证，而是在 `run-service.sh --supervise-child` 保留一个带 marker 的最小 shell parent，Redis 作为其直接 child 处于同一 setsid process group，信号被转发且仍能精确回收。
- job `771016` 在 `TimeLimit=01:00:00` 下用时 `00:03:09`，完成 restore 前后两轮 health/E2E 和 final checkpoint/teardown，Slurm 结果 `COMPLETED 0:0`，cleanup summary 为 `original=0`, `cleanup=0`。它的唯一 stderr 是进程刚退出时 `/proc/$pid/stat` 在 readability check 与 `awk` open 之间消失的 TOCTOU 噪声，不是 cleanup 失败；commit `083f448` 对这两个 read probe 将预期 race stderr 重定向，不放宽身份判定。
- 最终 job `771041` 在两次 stop 中遇到 FlowMesh 超过 45 秒 grace。脚本没有调用 `pkill`，而是只对已验证 deployment/start-time 的 process groups 升级 KILL；restore/复验继续成功，最终 cleanup `0/0`。这是 bounded grace -> verified-PGID escalation 的实机证据。

**证据边界 / Residual**：上述证明正常 EXIT 和两种启动失败都能保留原退出码且 cleanup 成功，但尚未主动向健康运行中的 allocation 注入真实 Slurm `TERM`，也没有等到 walltime 自然 timeout。因此“TERM@600 之后能在 KILL 前完成”必须保留为 Residual，不得由 normal EXIT 外推为已证明。

## 10. OBS-01：静态 health 与分层健康模型

### 10.1 是否所有 health 都必须验证 DB/S3/gRPC/worker

不应让每个 endpoint 每次都执行所有昂贵检查；应按语义分层：

| 层级 | endpoint/用途 | 应检查内容 | 不应做的事 |
|---|---|---|---|
| L0 Liveness | `/livez`，决定进程是否需要重启 | event loop/HTTP process 能响应 | 不访问 DB/S3/远端，避免依赖故障引发重启风暴 |
| L1 Readiness | `/readyz`，决定是否接流量/是否可提交工作 | 该服务完成请求所必需的直接依赖，短 timeout、并发 probe、503 + secret-free 状态 | 不执行完整模型 workflow，不返回 exception/DSN/token |
| L1 Stack readiness | `health.sh`，部署/值守 | DB authenticated `SELECT 1`、两个 S3 bucket、Redis authenticated `PING`、FlowMesh HTTP、gRPC 可用性、Lumilake、期望 cpu/gpu worker 数及 freshness/GPU subset | 不只检查端口 open 或静态 JSON |
| L2 Functional smoke | 启动后/发布前/定期但低频 | 一个最小 SQL workflow 经 Lumilake -> FlowMesh -> worker -> gateway -> DB/S3，并核对结果/provenance | 不作为高频 Kubernetes-style probe |

因此 DB、S3、gRPC、worker 对“整栈能否执行 workflow”都重要，但不必全部塞进每个进程的 `/livez`。直接依赖由各服务 `/readyz` 验证，`health.sh` 汇总，L2 再证明全链路语义。

### 10.2 原始问题

- gateway、FlowMesh、Lumilake 的 `/healthz` 基本返回静态 OK。
- `stack-up.sh`/`health.sh` 因而会在 DB/S3/Redis/FlowMesh gRPC 或所有 execution workers 已死时仍报告服务健康。
- 只看到 TCP/HTTP process 不代表 bearer 正确、bucket 有权限、DB 可查询、worker heartbeat 新鲜或 GPU 归属正确。

### 10.3 当前实现与状态

**analytics-agent gateway（Completed locally / committed）**

- 新 `/livez` 静态 liveness。
- `/readyz` 与兼容 `/healthz` 使用短 timeout，并发检查 PostgreSQL read-only `SELECT 1` 与 private/public S3 `HEAD Bucket`；失败返回 503，响应不回显 backend exception。
- 修改文件：
  - `analytics-agent/src/analytics_agent/lumid_gateway/app.py`
  - `analytics-agent/src/analytics_agent/lumid_gateway/config.py`
  - `analytics-agent/src/analytics_agent/lumid_gateway/db.py`
  - `analytics-agent/src/analytics_agent/lumid_gateway/storage.py`
  - `analytics-agent/tests/test_gateway.py`
  - `analytics-agent/README.md`
- 本地证据：`uv run pytest -q` -> **24 passed**（1 个既有 Starlette deprecation warning）；commit `e44f30d`。

**Lumilake（Completed locally / GitHub）**

- 新 `/livez`；`/readyz`/兼容 `/healthz` 并发 probe FlowMesh 与 lumid-data/gateway，使用 bearer 和 bounded timeout，失败 503 且不回显 exception。
- 修改文件：
  - `Lumilake/src/lumilake_server/health.py`
  - `Lumilake/src/lumilake_server/main.py`
  - `Lumilake/src/lumilake_server/hooks/security.py`
  - `Lumilake/src/lumilake_server/runtime/flowmesh_client.py`
  - `Lumilake/packages/sdk/src/lumilake/envs.py`
  - `Lumilake/packages/deploy/src/lumilake_deploy/assets/.env.example`
  - `Lumilake/docs/API.md`
  - `Lumilake/docs/ENV.md`
  - `Lumilake/tests/server/test_health.py`

- 本地完整 suite：**758 passed, 1 skipped**；Ruff、Black、mypy 通过。commits `1570600`、`38ba3e5` 已 push，PR：`https://github.com/ff98li/Lumilake/pull/1`。

**FlowMesh 与 stack health（代码、GitHub 与 SoC 集成 Completed）**

- FlowMesh 新 `/livez`；`/readyz`/兼容 `/healthz` bounded 并发检查 control/telemetry Redis、node/worker registries、supervisor IPC，并要求 `FLOWMESH_READY_MIN_WORKERS` 个非 stale IDLE/BUSY worker。修改 `routers/health.py`、`schemas/common.py`、API/ENV docs 与 `tests/server/test_health_router.py`。
- stack 将 `FLOWMESH_READY_MIN_WORKERS` 设为期望 CPU+GPU worker 数。`health.sh --level 0|1|2` 依次检查 process liveness；authenticated DB/Redis/MinIO policy/gateway/FlowMesh/Lumilake；最后检查 worker freshness、gRPC registration/heartbeat 证据、逻辑 GPU index/UUID/allocation 数量与精确 process ownership。
- `e2e-smoke.sh` 执行真实 `SELECT 1 -> gateway -> private MinIO materialization -> authenticated readback`，但它没有提交完整用户 workflow；完整 Lumilake -> FlowMesh task execution 仍应作为发布前低频 smoke。

**跨组件契约与实机证据**

初稿审阅一度发现 Lumilake probe 要求 dependency JSON 中 `ok is true`，而 gateway 只返回 `status: "ok"`。契约现已对齐：gateway readiness 同时返回 `ok: bool`，Lumilake 对两个 downstream `/readyz` 都要求 `ok=true`，并继续带各自 bearer。

job `771030` 是前一次精确部署版本复验：`TimeLimit=01:00:00`，用时 `00:02:43`，`COMPLETED 0:0`，stderr 为 0 bytes，cleanup `0/0`。stdout marker 统计为 `stack_up=2`、L2 `health L2: 19 ok, 0 failed` **6 次**、`E2E_SMOKE_OK=2`、`checkpoint committed=2`、`restore complete=2`。

最终签收 job `771041` 在 `xgpg0` 上以 `TimeLimit=01:00:00`、`Elapsed=00:03:22`、`COMPLETED 0:0` 再现同样的 `stack_up=2`、L2 19/19 **6 次**、E2E 2 次、checkpoint committed 2 次、restore complete 2 次，cleanup `0/0`，且 stderr 为 0 bytes。因此 readiness 不再只是两个仓库的 mock test，而是在启动、state-changing E2E、restore 重启后都实际走过。

这仍不等于“每个依赖都已分别做 kill/network-partition 故障注入”；DB/S3/Redis/gRPC/worker 各自故障时 503/退出契约的负向集成测试仍是可补强证据。

## 11. DATA-02：seed 幂等性

原 seed 只有 `instrument_profile.symbol` 是 primary key。其余表：

- `financial_income_statement`
- `ohlc_10m`
- `market_metrics`
- `news_metadata`

只有普通 index 或没有唯一约束。`INSERT ... ON CONFLICT DO NOTHING` 只有在违反 UNIQUE/PRIMARY KEY/exclusion constraint 时才会跳过；没有唯一约束时，每次 `stack-up.sh` 都会再次插入相同行。

“现有 checkpoint 仍是单份 seed，没有发生重复”的意思是：审计到的历史数据样本尚未观察到重复，并不代表脚本幂等。重复启动或 restore 后再 seed 才会触发问题，进而扭曲 SQL 聚合、workflow 输出与 benchmark。

**修复**

- 增加符合业务身份的唯一约束：例如 financial `(symbol,date)`、OHLC `(symbol,timestamp)`、metrics `(symbol,version)`、news `(symbol,title,publishedDate)`。
- seed 使用明确 `ON CONFLICT (<key>) DO UPDATE/NOTHING`，放在 transaction 中，`ON_ERROR_STOP=1`。
- 对已有 deployment 先检测/去重，再加约束；否则 migration 可能失败。
- 测试连续执行 seed 两次，比较各表 row count 与内容 checksum 不变。

**当前实现与证据**：`seed-demo-data.sql` 现在是 transaction，四个原先无约束表分别增加 natural-key UNIQUE index，并使用显式 `ON CONFLICT (...) DO UPDATE`；restore 后仍会再次执行以完成 schema reconciliation。jobs `771016` 与 `771030` 都在同一 allocation 内执行了两次 `stack-up.sh`，第二次是 restore 后再 seed；两轮 schema/catalog readiness 与 E2E 均成功，证明不会因重复 natural key 而失败或累加一份简单 seed。

若需要针对 benchmark 结果给出更强的 bit-for-bit 证据，还可在两次 seed 之间录制各表 row count 和排序内容 checksum；当前 job 标记证明了执行路径和唯一约束不发生冲突，不应误写成已录制了每张表的 checksum。

## 12. OPS-01：宽泛 `pkill -f`

原 `stack-down.sh` 通过以下模式兜底：

- `pkill -f "python -m worker.main"`
- `pkill -f "uvicorn analytics_agent.lumid_gateway"`
- `pkill -f "python -m server.main"`
- `pkill -f "python -m lumilake_server"`
- `pkill -f "minio server"`

`pkill -f` 匹配该 Unix 用户在整台节点上的完整 command line，不知道进程是否由本 allocation/stack 启动。如果同一账户在同节点运行另一个开发服务、测试 job，或 stale process 的命令刚好匹配，就会误杀。它也不能证明子进程组已被完整回收。

**正确实现**

- 每次启动记录 PID 与 PGID 到 mode `0600` 的 runtime pid directory，并写入 service、job id、node、start time/command fingerprint。
- child 使用独立 process group/session；停止时先验证 PID 仍属于当前用户、当前 allocation/stack root、start time 未复用，再对精确 PGID 发 TERM -> bounded wait -> KILL。
- tmux session 名应包含 job/stack identity，不要无条件复用通用 `lakehouse`。
- PostgreSQL 继续使用精确 `PGDATA` 的 `pg_ctl`；Redis/MinIO 使用自己的 pidfile/CLI；FlowMesh 先优雅停止 supervisor，让它回收 native worker groups。
- stale pidfile 只能在验证进程不存在后清理，不能回退到全局 pattern kill。

**实现与实机证据**：新增 `process-lib.sh`。每个非 PostgreSQL service 使用 `setsid`，记录 `0600` PID/PGID/`/proc` start-time/deployment id；停止前重新验证 owner context 与 PID reuse，不再按 command substring 搜索。TERM 有 bounded grace，之后只对启动前捕获且仍属于同 deployment 的精确 process groups 升级 KILL。PostgreSQL 继续由 allocation-specific `PGDATA/pg_ctl` 管理。

job `770992` 证明 Redis 5 会改写自身 environ，使原先对 Redis daemon PID 直接做 deployment-marker 验证失败。最终实现以 `run-service.sh --supervise-child` 作为 marker-bearing tracked parent，不放宽验证也不回退到 `pkill -f`。jobs `771016`、`771030`、`771041` 都在 restore 中停/启全栈一次，然后在 final cleanup 再精确回收一次，cleanup 均为 `0/0`；后两个 job stderr 为空。`771041` 两次 FlowMesh 都在 45 秒 grace 后只对 verified process groups 升级 KILL，证明 bounded escalation 可用而不是宽泛匹配杀进程。`083f448` 只消除进程正好退出时的 `/proc` read race 噪声，没有将未能验证的 PID 当作自己的进程去 kill。

离线 self-test 覆盖了相似 process/sentinel 不被误杀和 supervised child group 回收。实机 jobs 证明当前栈自身无残留；如果要把“实机相似 sentinel 在整个 stack-down 过程始终存活”作为独立验收项，还可补一个定向故障注入。

## 13. GPU-01/GPU-02：Slurm allocation 与 worker 生命周期

### 13.1 为什么必须保留 Slurm 原始 allocation token

Slurm/NVIDIA 环境中的 `CUDA_VISIBLE_DEVICES` 不保证是简单 host index；可能是：

- 数字 index：`2,5`
- GPU UUID：`GPU-...`
- MIG UUID：`MIG-...`

原 `ResourceManager` 试图把环境值全部 `int()`；UUID/MIG 会解析失败成空集合。即便是数字，NVML 通常不按 `CUDA_VISIBLE_DEVICES` 自动过滤，worker `hw.py` 又遍历 `nvmlDeviceGetCount()`，因此可能把整台宿主的 GPU 都上报给 FlowMesh。

这会造成两类错误：

- **漏报**：Slurm 已分配 GPU，但 supervisor 认为没有 GPU，worker 起不来。
- **越界/超卖**：worker/scheduler 看到不属于本 job 的 GPU，把任务调度到其他 Slurm 作业占用的设备，造成失败、数据干扰或资源违规。

**正确映射**

- supervisor 启动时保存 allocation token 序列，不把 UUID/MIG 强行转成 int。
- 内部 reservation 只在 allocation 的逻辑 slot `[0..n-1]` 上进行。
- 为某 worker 选择 slot subset 后，child 的 `CUDA_VISIBLE_DEVICES` 使用这些 slot 对应的 **原始 token subset**。
- child 内 CUDA 看到的是重新编号的逻辑 device `[0..k-1]`；worker hardware report 应只包含这 k 个设备，`index` 为逻辑 index，同时保留实际 UUID，绝不枚举/上报 allocation 外设备。
- explicit worker config 的 `cuda_devices` 应定义为 allocation-local logical slot；部署配置不应硬编码整机 physical index。

**实际修改文件（代码/测试/GitHub/SoC GPU 实机已验证）**

- `FlowMesh/src/server/supervisor/resource_manager.py`
- `FlowMesh/src/server/supervisor/adapters/native.py`
- `FlowMesh/src/worker/hw.py`
- 对 integer/UUID/MIG/subset/CPU-hide 的单元测试
- `cluster/scripts/stack/worker-config.yaml`（逻辑 slot 语义）

实现把 parent `CUDA_VISIBLE_DEVICES` 当作 opaque token list，建立 allocation-local slot -> raw ordinal/GPU UUID/MIG UUID map；native child 获得所选 raw subset，并用 `FLOWMESH_VISIBLE_GPU_TOKENS` 让 NVML 只解析该 subset、按逻辑 `[0..k-1]` 上报且保留 UUID。Docker adapter 也使用 parent allocation token；unit tests 覆盖 ordinal/UUID/MIG 与 subset。

job `771030` 的 mode `0600` GPU evidence 文件位于 `~/slurm/gpu-allocation-evidence-771030.json`。Slurm 原始 token 为 `0`，在该 allocation 上解析为 UUID `GPU-68c44577-3ce1-ade2-b7fc-4d2ddc48120c`；worker 上报的 logical index 为 `0`，UUID 与 Slurm token 解析结果精确一致。restore 前后的 L2 都通过同一 allocation-subset 不变量。这是“上报 allocation 子集而非整台宿主 GPU”的 SoC 实机证据，不再只是单元测试。

最终 job `771041` 再次生成 mode `0600` evidence：Slurm token 为 `0`，expected UUID 和 worker-reported UUID 都为 `GPU-29d30dd9-ece5-9394-7246-4fa4742ab0b3`，worker logical index 为 `0`。两个不同 GPU 节点上的精确一致结果为 allocation-local mapping 提供了比单次 job 更强的证据。

### 13.2 stop 失败时为什么不应释放 reservation

如果 `worker.stop()` 返回 false/抛错，进程可能仍在使用 GPU。原 manager 仍调用 factory `destroy_worker()`，它会 `deallocate_gpus()`，并且 registry removal/capacity update 可让 scheduler 认为 GPU 已空闲。随后新 worker 会获得同一 GPU，形成双重分配。

正确语义是：

- 只有确认进程/container/instance 已停止，才从 registry 删除并释放 reservation。
- stop 未确认时保留 worker record 和 reservation，标记 failed/quarantined/unhealthy，capacity 不能重新发布为 free。
- 运维人员可重试 stop 或做精确人工 recovery；不能为了“清爽的 registry”伪造资源已释放。

**当前实现（受控 lifecycle Completed；crash/restart quarantine Residual）**

- `FlowMesh/src/server/supervisor/manager.py` 已改为只对成功停止的 worker destroy/pop，并在失败时保留 registry 与 reservation；VastAI 未确认远端 shutdown 时也保留 offer reservation。
- `tests/server/test_worker_manager_gpu.py` 已加入 false/exception/registry/capacity tests，并包含在 702-test 主 hardening 基线中。jobs `771016`/`771030` 已验证受控启动、restore stop/start 和 final cleanup lifecycle。但 supervisor 异常 crash/KILL 后内存 quarantine 不持久，必须依赖 Slurm/cgroup cleanup 与 orphan reconciliation，不能把该场景声称为已解决。

## 14. TEST-01/TEST-02：历史验证脚本可信度

### 14.1 `07-flowmesh-check.sbatch`（Completed）

历史脚本的 Python heredoc 会打印多项 PASS，但最终引用未正确维护/定义的 `ok`，因此 job `740343` 在六项检查显示 PASS 后仍失败。修复包括：

- heredoc 明确初始化 `ok=True`，`check()` 用 `global ok` 聚合失败；
- 最终 `SystemExit(0 if ok else 1)`；
- shell 使用 `set -Eeuo pipefail`；
- `pytest ... | tail` 在 pipefail 下保留 pytest 退出码。

修改文件：`cluster/scripts/07-flowmesh-check.sbatch`。

### 14.2 `09-lumilake-dryrun.sbatch`（Completed）

历史 `command | tail` 返回的常常是 `tail` 的 0，而不是前面 CLI 的真实错误。jobs `740352/740358/740359/740360` 在包含 input/JSON/graph/job-list 错误时仍显示 Slurm `COMPLETED/0`，属于假成功；另一些预期 FlowMesh 不存在的 dry-run 边界又可能被当成假失败。

修复包括：

- 删除 `set -x`；使用 `set -Eeuo pipefail`。
- command 先完整写 log，单独捕获真实 rc，再 `tail` 展示；不靠 pipeline 状态猜测。
- 只 allowlist 精确的 FlowMesh `/api/v1/workers` connection failure；parse/validation/API 其他错误一律失败。
- dry-run 没有 gateway，因此移除误导性的 `job list` 检查。
- 保存 server PID，EXIT/TERM/INT 做精确 cleanup。
- 导出 `LUMILAKE_BASE_URL`，与随机/override server port 保持一致。

修改文件：`cluster/scripts/09-lumilake-dryrun.sbatch`。

### 14.3 验证证据

| Job | 限制/资源 | 结果 | 解释 |
|---|---|---|---|
| `770484` | CPU-only，<=1h | wrapper 非 0 | 07 的六项 Python checks 与 `6 passed` pytest 成功；09 暴露 CLI 仍指向 9000 的真实 port mismatch。这个失败是有效发现，不能算最终通过。 |
| `770489` | CPU-only，<=1h | `COMPLETED 0:0`，stderr 为空 | 修复 `LUMILAKE_BASE_URL` 后，07 与 09 都通过；两个 template 只在严格 allowlist 的 FlowMesh-unavailable 边界停止。 |

文件已部署到 SoC `~/slurm/07-flowmesh-check.sbatch` 与 `~/slurm/09-lumilake-dryrun.sbatch`；部署前备份：

- `~/slurm/07-flowmesh-check.sbatch.pre-codex-20260829`
- `~/slurm/09-lumilake-dryrun.sbatch.pre-codex-20260829`

`770489` 验证时的本地/远端 hash 为 07 `5024e10...`、09 `e1f29acf...`。后续 analytics commit `673a053` 只在 05d/07/08/09 入口加入当前 Slurm stdout/stderr `chmod 600`，因此当前 Git source-of-truth hash 已变为：07 `373d04dc2f6bae4b316534b6c4868f0eff10f89439a3764d33e36ddfc2ae865f`，09 `7e3dc7a1d5baf0509bae4ac9eab3e4f32ff3d8ce8c0ca8ab240c44160312f048`。job `771041` 已完成，新 log mode 为 `0600`、stderr 为空，且全栈 marker/cleanup 均通过。

`770489` 证明检查脚本自身的退出语义已修复；完整 GPU 栈的安全加固另由 jobs `771016`/`771030` 的全栈证据支持，不应混为同一类证据。

## 15. SLURM-01：非独占节点

`slurm-stack.sbatch` 申请 1 GPU、16 CPU、48 GiB，但没有 `#SBATCH --exclusive`。Slurm 可以把其他作业放在该节点剩余资源上；这些进程通常与本栈共享主机 network namespace，所以能尝试连接 loopback ports。

**处理选择**

- 最佳长期方案仍是每个服务有认证、最小权限和正确 bind；`--exclusive` 不能防同账户恶意进程、管理员或服务自身漏洞，也会浪费/占用整节点。
- 在无法及时为 gRPC/Redis等补齐强认证时，可把 `--exclusive` 作为 Phase 1 临时 defense-in-depth，但需确认课程/集群政策和 GPU 排队成本。
- 即使使用 `--exclusive`，仍应保留随机 secret、SCRAM/ACL、API auth、无 xtrace 和精确 PID cleanup。

**状态**：Residual/待明确决策。报告不把 loopback-only 等同于安全隔离。

## 16. GitHub、部署 source-of-truth 与两台 Mac 的路径

### 16.1 当前 Git ownership

- `analytics-agent/`：`git@github.com:ff98li/analytics-agent.git`，分支 `codex/soc-stack-hardening`，集群已部署且由 job `771041` 验证的 operational asset revision 为 `673a0531fa55dbc7e9cd809a0fa002f84c0c60de`，[PR #1](https://github.com/ff98li/analytics-agent/pull/1)。本报告后续提交会使 PR HEAD 继续前进，不影响该部署基线标识。
- `FlowMesh/`：fork `git@github.com:ff98li/FlowMesh.git`，分支 `codex/native-provider-hardening`，基于已推送的 `feat/native-worker-provider` (`f103a4f`)，当前 pushed HEAD `0b8be48e711515e566a5125ff56b67166a997473`，[PR #1](https://github.com/ff98li/FlowMesh/pull/1)；upstream `mlsys-io/FlowMesh` 不应被直接改写。
- `Lumilake/`：用户 fork `git@github.com:ff98li/Lumilake.git`，分支 `codex/readiness-hardening`，当前 pushed HEAD `38ba3e503f4e131c29a2c8e8ee510108f0f59af3`，[PR #1](https://github.com/ff98li/Lumilake/pull/1)；upstream 是 `mlsys-io/Lumilake`。
- 项目根目录本身不是 Git repo，`cluster/` 的 operational scripts 因而不会自动进入任何 GitHub remote。

### 16.2 第 9 项能否放 GitHub

可以，而且应该放。推荐 source-of-truth：

- 把 SoC deployment scripts（含 07/09、stack、workflow smoke、README）镜像到 `analytics-agent` 的明确目录，例如 `deploy/soc/`；
- FlowMesh/Lumilake 的产品代码分别留在各自 fork/branch；
- 本报告放在 `analytics-agent/docs/`，用 commit hash 和 deploy hash 把三仓库版本关联起来；
- Git 中只放模板和生成逻辑，绝不放 runtime secret file、Slurm stderr、checkpoint 或 token。

07/09、05d、完整 stack 与 smoke workflow 首次在 analytics commit `0d4d312` 进入 `deploy/soc/`；之后的 Redis 5 兼容、supervised wrapper、`/proc` race 与 Slurm log mode 修复分别由 `5528a1f`、`1d5de46`、`083f448`、`673a053` 追踪。FlowMesh patch series 为：

- `vendor/flowmesh/0001-native-worker-provider.patch`
- `vendor/flowmesh/0002-fix-harden-worker-control-plane.patch`
- `vendor/flowmesh/0003-fix-redact-redis-connection-failures.patch`
- `vendor/flowmesh/0004-fix-redact-redis-lifecycle-logs.patch`

`analytics-agent/deploy/soc/` 现为 Git source-of-truth，SoC 文件以 hash 对齐；runtime secret/log/checkpoint/GPU evidence 不进 Git。因此用户问到的“第 9 项能否一并放在 GitHub”的答案不仅是“可以”，而是已经完成，并有 PR 和 signed commits 可追踪。

### 16.3 MacBook Air 与 Mac mini 路径

- MacBook Air 实际 source workspace：`/Users/lifeifei/NUS_MComp/CP5105/`。
- MacBook Air 已存在软链：`/Users/lifeifei/NUS -> /Users/lifeifei/NUS_MComp`，所以文档中的 `/Users/lifeifei/NUS/CP5105/` 在 Air 上也可解析。
- `/Users/lifeifei/NUS/CP5105/` 同时是 Mac mini 上的真实项目路径；它不是“永久废弃路径”，而是 host-specific path。
- 后续通过 `ssh feifeis-mac-mini` 同步时应以 Git commit/hash 或显式 rsync source/destination 为准，不用“旧/新路径”判断内容新旧，且不得覆盖 Mac mini 未提交改动。

## 17. 本轮文件变更登记

以下列表按最终本地 commits 与 deployment mirror 分类；部署证据在 SoC 验证后追加。

### 17.1 Completed 且有 Slurm 证据

- `cluster/scripts/07-flowmesh-check.sbatch`
- `cluster/scripts/09-lumilake-dryrun.sbatch`

### 17.2 FlowMesh（Completed / GitHub PR #1 / SoC 已验证）

- `FlowMesh/src/server/supervisor/adapters/native.py`：固定 command/cwd/log path，拒绝 legacy extras。
- `FlowMesh/src/server/supervisor/adapters/base.py`：记录 supervisor 所见 heartbeat freshness。
- `FlowMesh/src/server/supervisor/adapters/docker.py`：allocation token 传递。
- `FlowMesh/src/server/supervisor/adapters/vastai.py`：stop 未确认保留 offer reservation。
- `FlowMesh/src/server/supervisor/manager.py`：三 provider 动态 allowlist；stop failure 保留 registry/reservation。
- `FlowMesh/src/server/supervisor/schemas.py`、`services/grpc_server.py`：在本地 worker info 中传播 REGISTER/HEARTBEAT freshness。
- `FlowMesh/src/server/supervisor/services/command_listener.py`、`supervisor.py`：失败 stop 不移除 lock，延长有界 grace，并让 gRPC 在 worker shutdown 期间保持可用。
- `FlowMesh/src/server/supervisor/resource_manager.py`、`FlowMesh/src/worker/hw.py`：opaque GPU token、logical slot 与 subset hardware report。
- `FlowMesh/src/server/auth/security.py`：可选的 fail-closed static bearer。
- `FlowMesh/src/server/env.py`：API key/Docker override/gRPC host env。
- `FlowMesh/src/server/config.py`：gRPC bind env。
- `FlowMesh/src/server/routers/health.py`、`FlowMesh/src/server/schemas/common.py`：分层 readiness。
- `FlowMesh/src/server/clients/redis.py`：sync/async connect/failure/flush lifecycle URL 脱敏，丢弃可含凭据的 exception text 与 chain。
- `FlowMesh/cli/stack/src/flowmesh_cli_stack/env_schema.py`、`assets/.env.example`。
- `FlowMesh/docs/API.md`、`ARCHITECTURE.md`、`ENV.md`。
- tests：`test_command_listener.py`、`test_config.py`、`test_hooks_wiring.py`、`test_worker_manager_gpu.py`、`test_health_router.py`、`test_native_worker_provider.py`、`test_redis_connection_logging.py`、`tests/worker/test_hw.py`。

### 17.3 analytics-agent（Completed / GitHub PR #1，已部署 operational revision `673a053`）

- `analytics-agent/src/analytics_agent/lumid_gateway/app.py`
- `analytics-agent/src/analytics_agent/lumid_gateway/config.py`
- `analytics-agent/src/analytics_agent/lumid_gateway/db.py`
- `analytics-agent/src/analytics_agent/lumid_gateway/storage.py`
- `analytics-agent/tests/test_gateway.py`
- `analytics-agent/README.md`
- `analytics-agent/.gitignore`（拒绝 runtime PID、checkpoint、secret 与 transport key/cert）
- `analytics-agent/docs/soc-cluster-stack-audit.md`（本文）
- `analytics-agent/deploy/soc/`（stack、05d/07/09 jobs、smoke workflow 与部署 runbook）
- `analytics-agent/vendor/flowmesh/0001-native-worker-provider.patch`
- `analytics-agent/vendor/flowmesh/0002-fix-harden-worker-control-plane.patch`
- `analytics-agent/vendor/flowmesh/0003-fix-redact-redis-connection-failures.patch`
- `analytics-agent/vendor/flowmesh/0004-fix-redact-redis-lifecycle-logs.patch`

### 17.4 Lumilake（Completed / GitHub PR #1 / SoC 已验证）

- `Lumilake/src/lumilake_server/health.py`
- `Lumilake/src/lumilake_server/main.py`
- `Lumilake/src/lumilake_server/hooks/security.py`
- `Lumilake/src/lumilake_server/runtime/flowmesh_client.py`
- `Lumilake/packages/sdk/src/lumilake/envs.py`
- `Lumilake/packages/deploy/src/lumilake_deploy/assets/.env.example`
- `Lumilake/tests/server/test_health.py`
- `Lumilake/tests/server/test_static_api_key_auth.py`
- `Lumilake/tests/runtime/server/test_flowmesh_client.py`
- `Lumilake/docs/API.md`
- `Lumilake/docs/ENV.md`

### 17.5 Cluster lifecycle（GitHub/SoC Completed；TERM/crash 故障注入 Residual）

- `cluster/scripts/stack/stack-env.sh`
- `cluster/scripts/stack/process-lib.sh`（新增）
- `cluster/scripts/stack/checkpoint.sh`（新增）
- `cluster/scripts/stack/run-service.sh`（新增）
- `cluster/scripts/stack/stack-up.sh`
- `cluster/scripts/stack/stack-down.sh`
- `cluster/scripts/stack/slurm-stack.sbatch`
- `cluster/scripts/stack/health.sh`
- `cluster/scripts/stack/seed-demo-data.sql`
- `cluster/scripts/stack/worker-config.yaml`
- `cluster/scripts/stack/e2e-smoke.sh`（新增）
- `cluster/scripts/stack/test-stack-scripts.sh`（新增）
- `cluster/scripts/05d-lumilake-groups.sbatch`
- `cluster/scripts/07-flowmesh-check.sbatch`
- `cluster/scripts/09-lumilake-dryrun.sbatch`
- `cluster/workflows/smoke-sql.yaml`（镜像进入 Git source-of-truth，内容未因本次安全修复改变）

变更链为 `0d4d312` 初始收录 -> `5528a1f` Redis 5 认证兼容 -> `1d5de46` Redis supervised identity -> `083f448` `/proc` 退出 race 静音 -> `673a053` Slurm log `0600`。镜像文件在 `analytics-agent/deploy/soc/`；jobs `771016`/`771030` 是前置实机 full-stack/restore 证据，`771041` 是最终 operational revision 签收。

## 18. 验证矩阵与完成标准

| 领域 | 已有证据 | 仍需证据 |
|---|---|---|
| Phase 1 功能 baseline | job `740372`，6/6 旧 health、Q2 11/11 nodes、MinIO archive | 旧 health 是静态/浅层，不能作为 hardening 证据 |
| 07/09 脚本 | job `770489` `COMPLETED 0:0`；stderr 空；严格 expected boundary；当前 assets 在 `673a053` 加 `0600` log mode；job `771041` 最终 full-stack 验证 logs `0600`/stderr 0 | 07/09 控制流修复已签收；后续修改它们时继续保留真实退出码回归 |
| gateway readiness | 本地全套 **24 passed**；jobs `771016`/`771030` 真实 PG + 两 bucket + 跨组件 readiness/E2E | 可选：依赖逐个 kill/network 负向故障注入 |
| FlowMesh native security | 主修复全套 **702 passed** 基线；当前 HEAD server **625 passed**；PR #1；job `771030` unauth 401/bearer 200 与 worker L2 | 完整 702 在 Redis follow-ups 后未重跑；不影响 625 server suite 的直接证据 |
| Docker/VastAI security | fail-closed allowlist/rejection/stop tests 已通过 | image digest、云配额/egress policy |
| GPU | ordinal/UUID/MIG/subset/invalid-token unit tests；jobs `771030`/`771041` 分别将 token `0` 精确对齐到 worker logical `0` 和两个不同 expected UUID；evidence `0600` | 多 GPU/MIG 实机可作扩展测试，当前 1-GPU Phase 1 不受阻 |
| stop failure | manager/Docker/native/Vast quarantine fault tests；jobs `770815`/`770992` 失败 cleanup `1/0`；`771041` 两次 45s 后只 KILL verified PGIDs，restore/final cleanup `0/0` | supervisor crash 后 quarantine 持久化/orphan reconciliation |
| secrets/auth | offline 0600/uniqueness scans；job `771041` SCRAM/no-trust、Redis auth+unauth rejection、API/TLS、MinIO policy；stderr 0；new log/archive secret scans=0；historical group/world-readable logs=0 | 历史凭据仍按已泄漏处理，权限收紧不代替轮换 |
| checkpoint/restore | jobs `771016`/`771030`/`771041` atomic create/validate，停 stores，同 allocation restore，两轮 L2/E2E，final checkpoint | 非-seed DB + 两 bucket 预存 sentinels 的内容 checksum 可选补强；periodic consistency residual |
| timeout cleanup | 启动失败 jobs `770815`/`770992` 保留 original=1/cleanup=0；正常 jobs `771016`/`771030`/`771041` cleanup 0/0；`771041` 验证 bounded KILL escalation | 真实 `scancel --signal=TERM`/自然 timeout 故障注入；不用 normal EXIT 代替 |
| health L0/L1/L2 | gateway 24；FlowMesh 702 基线 + 625 当前 server；Lumilake 758/1；jobs `771030`/`771041` 均 L2 19/19 六次、E2E 两次 | 完整用户 workflow 非本次 minimal E2E 范围；逐依赖负向注入可补 |
| seed | restore 前后两次 stack-up/seed 并通过 schema/E2E | 若需 bit-for-bit benchmark 证据，补各表 row count + sorted checksum |
| PID cleanup | offline similar-sentinel/supervised-child tests；job `771041` 两次 teardown 与 final cleanup，FlowMesh 两次只升级 verified PGIDs；active stack jobs=0；`/proc` race 已修 | 相似 sentinel 实机故障注入可选补强 |

## 19. Rollout 实际完成状态

原计划的前 9 步已执行：随机 `0600` secrets、FlowMesh provider/API/GPU/stop 加固、PostgreSQL SCRAM、version-aware Redis auth、MinIO least privilege、PID/PGID lifecycle、checkpoint/restore、seed 幂等、分层 health/E2E、<=1h GPU drills、GitHub PR/source-of-truth 和日志权限收紧均已有代码与证据。最终 job `771041` 后 active stack jobs 为 0，部署内容 checksum dry-run 无差异，未为审计启动 3-day job。

后续 rollout 应聚焦 Residual：

1. 用独立 <=1h job 对健康 stack 注入真实 Slurm `TERM`，验证 `TERM@600` 与原始 143 语义。
2. 根据集群政策决定是否加 `--exclusive`；若保持共享节点，继续把 loopback 当作同节点可达。
3. 为 supervisor-crash orphan reconciliation、跨节点 mTLS、VastAI 预算/egress、checkpoint retention/consistency 指定 owner/deadline。
4. 若需 bit-for-bit recovery 证据，补非-seed DB/两 bucket sentinels 与 seed row-count/checksum drill。

## 20. 剩余风险与接受条件

- **FlowMesh bearer 粗粒度**：单一静态 token 适合 Phase 1 单租户最低线；多用户需要 IdentityProvider + PermissionChecker/RBAC、token scope/expiry/rotation。
- **gRPC**：本地 stack 已加 per-deployment TLS server certificate + random worker-token RPC auth；未来跨节点/多租户仍应升级为可撤销的每-node/worker identity，优先 mTLS。
- **VastAI**：request allowlist 已在本地实现，但预算/配额、trusted image digest、region 与 egress policy 完成前仍应限制为管理员。
- **Docker host mounts/socket**：动态 allowlist、suite 与 push 已完成；operator-owned SSH/socket 配置本身仍等价于高权限边界，只能由隔离的管理员控制面开启。
- **native worker 无 sandbox**：它与 supervisor 同 Unix 用户；只能给高度受信的 workload/模型/插件使用。
- **supervisor crash quarantine 不持久**：stop false/exception 时的 in-memory reservation 已修，但 supervisor 本身异常 crash/KILL 后仍需 Slurm/cgroup cleanup 与 orphan reconciliation。
- **真实 Slurm TERM/timeout**：已验证启动失败、normal EXIT 和 45s 后 verified-PGID KILL escalation；尚未对健康 stack 注入 Slurm `TERM` 或等待自然 timeout。
- **Redis in-flight state**：当前恢复目标只覆盖 PostgreSQL/MinIO durable data；作业中断后应重新提交，不能承诺透明续跑。
- **MinIO privilege**：gateway 已改用 bucket-scoped app identity；root 只在 bootstrap 时存在于最小化环境。该 app 对两个目标 bucket 仍有读写/删除权，gateway compromise 会影响这两个 bucket。
- **checkpoint consistency/retention**：periodic PG/S3 snapshot 不是同一跨存储 transaction，且 generations 尚无上限；优先使用 quiesced final，增加 manifest 语义与安全 retention。
- **checkpoint at rest**：NFS home `0700` 是访问控制，不等于加密；若包含敏感数据，需要 retention、加密和备份策略。
- **恢复/幂等证据粒度**：实机已走 create/validate/restore/两次 E2E，但未录制非-seed DB/两 bucket sentinels 和每表 sorted checksum。
- **Slurm non-exclusive**：若保留共享节点，所有 loopback services 必须当作同节点可达服务来加固。
- **跨节点扩展**：在 TLS、firewall、service identity 完成前，不应把单节点 stack 简单改为 `0.0.0.0` 后跨站运行。

## 21. 最终签收判定

**本轮 Phase 1 单节点栈 hardening 已签收：Completed with documented residuals。**

- 三仓库 commits/PR、deployment source-of-truth 与 FlowMesh `0001..0004` patch series 可追踪；SoC checksum dry-run 无差异。
- CPU job `770489` 与最终 GPU/full-stack job `771041` 都在 1 小时上限内 `COMPLETED 0:0`；后者用时 `00:03:22`，stderr 0 bytes，logs `0600`，markers 与 cleanup `0/0`。
- `771041` 的 GPU token `0` 与 expected/reported UUID `GPU-29d30dd9-ece5-9394-7246-4fa4742ab0b3` 一致；restore 前后共六次 L2 19/19、两次 E2E、两次 checkpoint/restore。
- 新 log/archive 的 secret assignment、raw credential URL、bearer scan 均为 0；历史项目 logs 已 `0600`，group/world-readable 为 0，当前 active stack jobs 为 0。

该签收不暗示已解决真实 Slurm TERM/timeout 注入、supervisor-crash quarantine 持久化、multi-node mTLS、VastAI 云治理、Slurm exclusive、Redis in-flight 恢复或跨存储原子一致性；它们保留为第 20 节 Residual。

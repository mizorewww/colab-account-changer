# Colab Account Changer

**中文 | [English](README.en.md)**

Google Colab CLI 多账号管理工具，支持账号切换、计算单元用量查询，以及跨账号的会话资源池。

## 安装

需要 Python 3.12+、[uv](https://docs.astral.sh/uv/) 和 `google-colab-cli>=0.8.0`。在项目目录执行：

```bash
uv tool install "google-colab-cli>=0.8.0"
uv tool install .
```

如果已安装 Colab CLI，可用 `uv tool upgrade google-colab-cli` 升级。

## 使用

### 1. 导入账号并查看用量

```bash
# 自动导入 Colab CLI 中已有的账号，并在线刷新用量
colab-account list --usage

# 查看账号和缓存用量，不发起用量请求
colab-account list

# 刷新当前账号或指定账号的用量
colab-account usage
colab-account usage work
```

列表的 **Total** 是纳入计算的账号余额与消耗速率之和；覆盖不完整时显示 **Known total**，不会把未知余额当作 0。实时刷新时只合计本轮成功且凭据存在的账号，失败账号的旧缓存会标为 `stale` / `no credential` 并排除；`--cached` 模式则明确按本地缓存合计。JSON 的 `usage_mode` 和每账号 `usage_in_total` 表示同一口径。**ETA** 按 `余额 ÷ 当前 CU/hr` 线性估算，当前速率为 0 且仍有余额时显示 `∞`。该估算会随实例和用量变化，并非耗尽时间保证。

### 2. 添加并切换账号

```bash
# 按提示完成 Google 授权，将账号保存为 work
colab-account add work

# 切换账号
colab-account use work

# 查看当前账号
colab-account current
```

其他账号操作：

```bash
colab-account sync          # 重新导入已有 Colab OAuth 凭据
colab-account save work     # 将当前账号保存为 work
colab-account remove work   # 删除非活动账号
```

### 3. 按名称管理 Colab 会话

资源池会自动切换到会话所属账号，无需手动维护账号与 VM 的对应关系。`colab-pool` 等同于 `colab-account pool`。

```bash
# 新建会话：总是建在主账号上
colab-pool new node-x --gpu g4

# 幂等新建：名称已登记且服务端仍在则什么都不做（退出码 0）；已确认消失则清理后重建
colab-pool new --if-missing node-x --gpu g4

# 在指定会话中执行命令或上传文件
colab-pool exec node-x -f train.py
colab-pool upload node-x data.tgz /content/data.tgz

# 查看所有已保存账号的服务端实例、各账号用量和主账号
colab-pool list

# 仅跳过用量刷新；实例清单仍逐账号实时查询
colab-pool list --cached

# 停止并注销会话
colab-pool stop node-x

# 一次确认后关闭所有账号下的池内实例和未登记实例，并重新核验
colab-pool stop-all

# 非交互脚本中显式确认全部关闭（会丢失所有实例上的未保存工作）
colab-pool stop-all --yes

# 检查未登记或已消失的会话
colab-pool reconcile

# 列出既未登记、也不在服务端的残留 Colab 记录；加 --yes 才删除
colab-pool prune-state
```

`stop-all` 会逐个账号处理，单个实例失败不会阻止其他实例；最多进行三轮以处理失败或新发现的实例。只有服务端无实例、无账号查询错误且本地池登记清空时才报告成功，否则列出剩余 endpoint/账号并返回非零。后台启动器仍可能继续创建新实例，本命令不会杀死本地进程。

`list` 的实例表包含 `pooled`（与本地登记匹配）、`orphan`（服务端存在但未登记）和 `vanished`（本地登记已不在服务端）。名称和登记时间来自本地；endpoint 和在线硬件来自服务端查询。表头显示成功检查的账号数，任一账号检查失败时命令返回非零，避免把查询失败误认为零实例。`--json` 输出中的 `server_inspection` 提供同样的覆盖率和错误信息。Colab CLI 在登录失败时也可能打印“无会话”并返回 0，因此空清单只有在该账号的用量接口（ccu-info）也报告 0 个分配时才被采信；用量请求失败、分配数大于 0 或输出无法解析时，都按该账号检查失败处理（`reconcile` 记入 `errors`，`stop-all` 不报告成功，自动清理也不会注销登记）。

资源池按**单账号策略**运行：`new` 只在主账号上创建会话。只有在主账号余额**已确认**低于阈值（默认 5 CU）时，才换到余额最高的账号；用量未知或刷新失败时保持原主账号，绝不因“未知”而换。主账号变化会打印一行说明，并记入登记表的 `events`。`--account` 只能指向主账号（未设主账号时会把它设为主账号）；指向其他账号默认拒绝，确需例外时加 `--allow-other-account`（会打印警告，主账号不变）。

主账号满了（Colab 拒绝分配：并发、配额或容量）或达到下面的数量上限时，`new` 以**退出码 69** 失败，并说明该账号已有几个会话。这时请停掉一个会话或稍后重试，**不要换账号**。所请求的加速器无权使用时则以普通错误（退出码 1）提示换加速器。

不再使用的账号可以禁用：自动选择永不选它，它也不能成为主账号（禁用当前主账号时会清空主账号，下次 `new` 按规则另选并提示）；`new --account`（即使加 `--allow-other-account`）、`adopt`、`import` 指向它都会被拒绝。`stop`、`reap`、`reconcile`、`list`、`stop-all` 以及已登记会话的按名称命令仍覆盖它，方便取回结果并停机；`list` 标出 `disabled` 和原因，`list`/`reconcile` 会提示仍留在禁用账号上的会话。禁用不会删除凭据。

```bash
colab-pool disable old-account --reason "quota exhausted"
colab-pool enable old-account
```

可手动设置主账号和切换阈值：

```bash
colab-pool primary work --threshold 3
```

`new` 有数量上限，防止“失败就重试”的循环无限建机器：默认每个账号最多 4 个（即主账号上实际能开的台数），全池最多 8 个（为其他账号上尚未清理的会话留余量）。计数以登记表为准；目标账号的服务端清单可信时，该账号上未登记的 VM 也计入。超限时拒绝并列出当前会话。查看或修改上限：

```bash
colab-pool limits
colab-pool limits --max-sessions 16 --max-per-account 6
```

## 会话失联与恢复

- 在任务代码和启动脚本准备好后再创建 VM，并立即上传、执行，避免新建实例长时间空闲。
- 按名称执行时直接传 Colab 参数，例如 `colab-pool exec node-x -f train.py`；`exec` 的名称后不要额外插入 `--`。
- `404/401 ... Cleaning up` 表示 Colab CLI 无法连接运行时并删除了本地记录。命令失败后，资源池会复查所属账号的服务端；确认 endpoint 已消失时自动注销旧登记和本地记录，并提示先看 `colab-pool reconcile`；需要替代机器时再用原名称 `new`（或 `new --if-missing`）。
- 如果服务端核对失败或 endpoint 仍存在，资源池会保留登记，避免留下无法管理的 VM。先运行 `colab-pool reconcile --all` 检查，不要盲目重复执行或重建。
- 对已确认消失但仍保留的旧登记，运行 `colab-pool stop NAME` 清理；即使 `colab stop` 失败，只要服务端确认 endpoint 已不存在，也会注销登记。
- 注销已确认消失的会话（自动清理、`stop`、`stop-all`）时，资源池同时删除该账号 state 文件中的同名 Colab 记录，名称可直接复用。`stop --forget` 不联系 Colab，会保留记录；之后 `new` 同名时先向服务端核实：已消失则清掉旧记录后创建，仍存在则拒绝并提示 `adopt`/`reap`，无法核实则拒绝。`colab-pool prune-state` 报告所有账号中这类残留记录，`--yes` 才删除，仍在服务端的记录永不删除。
- `colab new` 失败但 VM 已分配（state 文件中已有 endpoint）时，资源池会立即尝试释放并向服务端确认；释放或确认失败则把它登记下来并在错误信息中说明，可用 `colab-pool stop NAME` 释放，不会留下孤儿。`colab new` 返回 0 却没有写入记录时同样报错，并提示先用 `reconcile` 查找可能的孤儿。
- 账号锁按读写锁工作：目标账号已是当前账号时，`exec`/`upload`/`download`/`ls`/`status` 等按名称执行的命令共享锁、可并发；需要切换账号的命令以及 `new`/`stop`/`reap`/`reconcile`/`list`/`stop-all` 独占锁，并且排队等待的独占者优先于后来的共享者，不会被饿死。`colab-pool status`（不带名称）在不等锁的情况下显示当前持锁者；单条命令持锁超过 120 秒（`--lock-warn-after` / `COLAB_POOL_LOCK_WARN_AFTER`，0 关闭）时在 stderr 提示一次，不会终止命令。
- 等锁超时（退出码 75）只表示资源池正忙，错误信息会列出持锁者、持有时长与子命令/会话名；**这不等于会话丢失，不要因此重建机器**，稍后重试即可。
- 不要用 `colab-pool upload`/`download` 传大文件：传输期间一直持锁，会阻塞所有需要切换账号的命令。大文件请走 Google Drive（`drivemount`）或在 VM 内直接下载。
- 不要混用原始 `colab` 与 `colab-pool` 操作同一个会话；原始命令不持有资源池的账号锁。

## 注意

- 仅管理 Colab CLI 的默认 OAuth2 凭据，不切换 ADC 或 gcloud 登录；凭据保存在操作系统 keyring 中。
- 切换账号不会停止、创建或迁移已有 Colab VM；需要跨账号管理 VM 时请使用资源池。
- 运行 `colab-account --help`、`colab-account COMMAND --help` 或 `colab-pool --help` 查看全部选项。

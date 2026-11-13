# Colab Account Changer

**中文 | [English](README.en.md)**

Google Colab CLI 多账号切换工具。支持自动读取已有账号、切换账号，以及查看计算单元余额和消耗速率。

## 环境要求

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- `google-colab-cli>=0.8.0`

升级 Colab CLI：

```bash
uv tool upgrade google-colab-cli
colab version
```

## 安装

在本项目目录执行：

```bash
uv tool install .
colab-account --help
```

也可以在开发目录直接运行：

```bash
uv sync
uv run colab-account --help
```

如果清华 PyPI 镜像暂时不可用：

```bash
UV_DEFAULT_INDEX=https://pypi.org/simple uv sync
```

## 快速开始

### 1. 读取已有账号并刷新用量

```bash
colab-account list --usage
```

首次运行会自动读取 Colab CLI 的 keyring 和旧版 `~/.config/colab-cli/token.json`，导入已有账号并查询用量。

### 2. 添加新账号

```bash
colab-account add work
```

按照终端提示完成一次 Google 授权。授权成功后，账号会保存为 `work`，之后切换无需重新登录。

如果 `colab` 不在 `PATH` 中：

```bash
colab-account add work --colab-command /absolute/path/to/colab
```

### 3. 切换账号

```bash
colab-account use work
```

查看当前账号：

```bash
colab-account current
```

### 4. 查看账号和用量

```bash
# 直接读取上次缓存，不发起 usage 请求
colab-account list

# 在线刷新全部账号的用量并更新缓存
colab-account list --usage

# 只读本地 profile 和缓存，跳过自动发现
colab-account list --no-discover

# 刷新当前账号的用量
colab-account usage

# 刷新指定账号的用量
colab-account usage work
```

普通 `list` 会显示余额、消耗速率、活动分配数和缓存时间。缓存不存在时显示 `—` 和 `never`。

## 资源池

一个 Colab 会话只在创建它的账号下可见；在别的账号下执行 `colab sessions`/`colab exec`，Colab CLI 会清掉"看不到"的本地会话记录。资源池把多个账号当成一个池子：只按**会话名字**建机器、用机器、停机器，不用关心它在哪个账号上。`colab-pool` 等价于 `colab-account pool`。

```bash
# 建机器：自动选账号并登记（colab new 的参数直接跟在后面）
colab-pool new node-x --gpu g4
colab-pool new node-y --account work      # 指定账号

# 按名字使用：自动切到所属账号；参数、stdin 和退出码原样透传
colab-pool exec node-x -f train.py
echo 'print(1)' | colab-pool exec node-x
colab-pool upload node-x data.tgz /content/data.tgz
colab-pool cmd edit node-x /content/a.py  # 任意按 -s 选会话的 colab 子命令

# 停机器并注销（服务器上已不存在也能注销）
colab-pool stop node-x

# 登记表 + 各账号余额/速率/机器数 + 主账号
colab-pool list
colab-pool list --json --cached
```

选账号：钉住一个主账号一直用，余额低于阈值（默认 5 CU）才换到余额最多的账号并改钉。

```bash
colab-pool primary                 # 查看
colab-pool primary work --threshold 3
```

对账与清理：

```bash
# 对照服务器：没登记的机器（孤儿）、登记了但已消失的会话；不一致时退出码为 1
colab-pool reconcile               # 登记过的账号 + 用量显示有机器的账号
colab-pool reconcile --all --json  # 逐个检查全部账号

# 停掉孤儿机器（需确认，--yes 跳过）
colab-pool reap work gpu-t4-s-xxxxxxxx

# 把池子外建的会话收进来
colab-pool adopt node-z work /path/to/sessions.json

# 从旧版 pool 脚本导入（只读，不修改原文件）
colab-pool import /path/to/tools/pool.json
```

旧脚本使用自己独立的锁，导入后请停用它，只用 `colab-pool`。

工作方式：

- 登记表 `pool.json` 和每个账号独立的会话记录 `pool-state/ACCOUNT.json` 与 `config.json` 放在同一目录，权限仅限本人，原子写入；不保存任何令牌。
- 每条命令先取账号锁（与 `colab-account use` 共用 `config.lock`），切到所属账号，执行 `colab --config pool-state/ACCOUNT.json SUB -s NAME ...`，结束后再放锁。所以并发命令会排队执行，各自都在正确的账号下。
- 等锁超时（默认 600 秒，`--lock-timeout` 或 `COLAB_POOL_LOCK_TIMEOUT`）以退出码 75 结束，可以稍后重试。
- 持锁期间别人在等：单条命令要短，长任务请在 VM 上 `nohup` 运行，本地短轮询。`repl`/`ssh` 会在整个交互期间持锁。
- `--offline`（或 `COLAB_POOL_OFFLINE=1`）切换时跳过在线身份验证；`new`、`list`、`reconcile` 的 `--cached` 使用缓存余额。

## 常用命令

| 命令 | 用途 |
| --- | --- |
| `colab-account list` | 列出账号并显示缓存用量 |
| `colab-account list --usage` | 刷新全部账号用量 |
| `colab-account current` | 查看当前账号，不访问 Google |
| `colab-account sync` | 导入已有 Colab OAuth 凭据 |
| `colab-account add NAME` | 登录并添加新账号 |
| `colab-account use NAME` | 切换到指定账号 |
| `colab-account save NAME` | 将当前账号保存为指定别名 |
| `colab-account usage [NAME]` | 刷新当前或指定账号的用量 |
| `colab-account remove NAME` | 删除非活动账号 |
| `colab-pool new NAME` | 在池子里建会话 |
| `colab-pool exec NAME ...` | 按名字在所属账号上执行 |
| `colab-pool stop NAME` | 停会话并注销 |
| `colab-pool list` | 查看会话、余额和主账号 |
| `colab-pool reconcile` | 对照服务器检查孤儿和已消失的会话 |

## 离线操作

```bash
colab-account sync --offline
colab-account list --offline
colab-account save work --offline --email you@example.com
colab-account use work --offline
```

`--offline` 只跳过在线身份验证，usage 查询仍需要网络。离线邮箱标签不代表已验证身份。

如果离线保存时需要替换一个身份无法确认的已有 profile，请先在线保存，或明确使用：

```bash
colab-account save work --force
```

## 其他选项

```bash
# 切换时不更新当前 profile 的已保存凭据
colab-account use work --no-save

# 明确允许丢弃未保存的当前凭据（危险）
colab-account use work --discard-unsaved

# 删除账号时跳过确认
colab-account remove work --yes
```

## 注意事项

- 仅管理 Colab CLI 默认的 OAuth2 凭据，不管理 ADC 或 gcloud 登录。
- 凭据保存在操作系统 keyring 中；旧版 `token.json` 只读，不会被修改。
- 切换账号不会停止、创建或迁移已有 Colab VM；需要按名字管理 VM 时使用资源池。
- 切换前如需清理运行时，请自行使用 `colab sessions` 和 `colab stop`。

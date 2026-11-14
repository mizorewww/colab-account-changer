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
# 新建会话，自动选择账号；也可通过 --account 指定账号
colab-pool new node-x --gpu g4
colab-pool new node-y --account work

# 在指定会话中执行命令或上传文件
colab-pool exec node-x -f train.py
colab-pool upload node-x data.tgz /content/data.tgz

# 查看会话、各账号用量和主账号
colab-pool list

# 停止并注销会话
colab-pool stop node-x

# 检查未登记或已消失的会话
colab-pool reconcile
```

资源池默认持续使用主账号，主账号余额低于 5 CU 时切换到余额最高的账号。可手动设置主账号和切换阈值：

```bash
colab-pool primary work --threshold 3
```

## 会话失联与恢复

- 在任务代码和启动脚本准备好后再创建 VM，并立即上传、执行，避免新建实例长时间空闲。
- 按名称执行时直接传 Colab 参数，例如 `colab-pool exec node-x -f train.py`；`exec` 的名称后不要额外插入 `--`。
- `404/401 ... Cleaning up` 表示 Colab CLI 无法连接运行时并删除了本地记录。命令失败后，资源池会复查所属账号的服务端；确认 endpoint 已消失时自动注销旧登记，然后可用原名称重新 `new`。
- 如果服务端核对失败或 endpoint 仍存在，资源池会保留登记，避免留下无法管理的 VM。先运行 `colab-pool reconcile --all` 检查，不要盲目重复执行或重建。
- 对已确认消失但仍保留的旧登记，运行 `colab-pool stop NAME` 清理。不要混用原始 `colab` 与 `colab-pool` 操作同一个会话；原始命令不持有资源池的账号锁。

## 注意

- 仅管理 Colab CLI 的默认 OAuth2 凭据，不切换 ADC 或 gcloud 登录；凭据保存在操作系统 keyring 中。
- 切换账号不会停止、创建或迁移已有 Colab VM；需要跨账号管理 VM 时请使用资源池。
- 运行 `colab-account --help`、`colab-account COMMAND --help` 或 `colab-pool --help` 查看全部选项。

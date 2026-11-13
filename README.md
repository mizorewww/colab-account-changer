# Colab Account Changer

基于 **Python + uv + Typer + Rich** 的 Google Colab CLI 多账号切换器。

适配 [google-colab-cli](https://github.com/googlecolab/google-colab-cli) 的默认 `oauth2` 认证方式。自动读取系统钥匙串和旧版 `~/.config/colab-cli/token.json`，按 Google 邮箱生成账号别名，并可直接查询每个账号的计算单元余额与消耗速率。

令牌只保存在操作系统钥匙串中。本地配置仅记录别名、邮箱、来源、时间和 SHA-256 摘要，**不记录访问令牌、刷新令牌或 client secret**。

## 功能

- `sync` / `list`：自动发现并导入已有 OAuth 账号，相同邮箱自动去重。
- `usage` / `list --usage`：直接查询余额、每小时消耗和活动分配数，无需切换账号。
- `add`：保存当前账号后调用 Colab CLI 登录新账号；失败或 Ctrl-C 时恢复。
- `use`：自动保存当前账号并切换；写入后回读校验，失败时回滚。
- `save`：为当前登录指定自定义别名。
- `current`：纯本地发现并查看当前账号，不访问 Google。
- `remove`：确认后删除非活动账号。
- 使用文件锁防止并发切换，配置采用原子写入和私有文件权限。

## 环境要求

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- `google-colab-cli>=0.8.0`：支持钥匙串 OAuth 凭据和 `colab whoami`
- 可用的系统钥匙串（macOS Keychain、Windows Credential Locker 或 Linux Secret Service）

旧版 `~/.config/colab-cli/token.json` 只读且不会被此工具删除或覆盖。没有钥匙串令牌时，它会成为当前账号的回退凭据。添加新账号时，Colab 子进程使用隔离的临时 `HOME`，防止旧文件被意外迁移。

`add` 要求 Colab CLI 0.8.0 或更高版本；0.7.x 只写普通 token 文件，不受支持。可先升级：

```bash
uv tool upgrade google-colab-cli
colab version
```

## 安装

在本仓库中安装为 uv 工具：

```bash
uv tool install .
colab-account --help
```

或直接运行开发版本：

```bash
uv sync
uv run colab-account --help
```

项目默认使用清华 PyPI 镜像。如果镜像暂时不可用，可只对单次命令回退到官方源：

```bash
UV_DEFAULT_INDEX=https://pypi.org/simple uv sync
```

## 快速开始

### 1. 直接读取已有账号和用量

```bash
colab-account list --usage
```

首次运行会自动：

1. 读取 Colab CLI 当前钥匙串条目。
2. 读取旧版 `token.json`（如果存在），但不修改原文件。
3. 在线验证新发现账号的 Google 身份，并按邮箱本地部分生成别名。
4. 将各账号凭据保存到独立的系统钥匙串条目。
5. 使用每个凭据调用只读的 `GET /tun/m/ccu-info`，展示用量。

示例列：`Alias`、`Email`、`Balance (CU)`、`CU/hr`、`VMs`。

也可以分步执行：

```bash
# 只导入已有记录
colab-account sync

# 查看账号，不查询用量
colab-account list

# 查询活动账号或指定账号
colab-account usage
colab-account usage personal
```

`sync --offline` / `list --offline` 可跳过新账号的在线身份验证。此时使用 `current` / `legacy` 等本地别名，邮箱显示为未验证；后续在线 `list` 会补全邮箱。usage 查询本身仍需要网络。`list --no-discover` 只显示已导入的 profile。

离线发现只在 refresh token 相同时才把变化的当前凭据视为同账号刷新。如果 refresh token 不同，会新建未验证 profile，绝不覆盖旧账号。`save --offline` 遇到不同 refresh token 也会停止，要求先在线验证或显式使用 `--force`。

`gcloud auth list` 中的账号也会被读取。只有 gcloud 名称而没有 Colab OAuth 凭据的账号会单独标注为“不可切换、不可查询 Colab usage”，不会与有效 Colab 账号混淆。

### 2. 添加另一个账号

```bash
colab-account add work
```

该命令会：

1. 自动发现并保存当前账号的最新凭据。
2. 暂时清空 Colab CLI 的当前钥匙串 OAuth 条目（profile 副本不受影响）。
3. 执行 `colab --auth=oauth2 whoami`，由 Colab CLI 完成浏览器授权。
4. 确认新令牌确实写入钥匙串，再保存为 `work` 并设为活动账号。
5. 登录失败、凭据无效或用户按下 Ctrl-C 时恢复原账号。

Colab 可执行文件不在 `PATH` 时：

```bash
colab-account add work --colab-command /absolute/path/to/colab
```

### 3. 查看和切换

```bash
colab-account list
colab-account current
colab-account use personal
colab-account use work
```

切换前默认重新保存当前 profile，以保留 Colab CLI 刷新过的令牌。可用 `--offline` 跳过在线刷新和身份检查：

```bash
colab-account use work --offline
```

对已跟踪的当前账号，可使用 `--no-save` 保留其上次保存的副本。如果当前凭据无法识别或没有副本，只有明确确认它可丢弃时才使用：

```bash
colab-account use work --discard-unsaved
```

**不要随意使用 `--discard-unsaved`。** 此模式不会自动保存未跟踪的当前凭据，被替换后可能无法恢复。

### 4. 自定义当前账号别名

```bash
colab-account save personal
```

默认刷新过期令牌并通过 Google `tokeninfo` 获取邮箱。无网络时可以指定人工标签：

```bash
colab-account save personal --offline --email you@example.com
```

人工邮箱标签不代表已验证身份。如果已有 profile 的邮箱与当前已验证身份不同，`save` 会拒绝覆盖；确认需要替换时使用：

```bash
colab-account save work --force
```

### 5. 删除账号

```bash
colab-account remove work
# 跳过交互确认
colab-account remove work --yes
```

活动 profile 不能直接删除，请先切换账号。删除操作不会登出或清除 Colab CLI 当前凭据。

## 重要边界

- **只切换 CLI 的默认 OAuth2 凭据。** `colab --auth=adc` 使用的 Application Default Credentials 和 `GOOGLE_APPLICATION_CREDENTIALS` 不受此工具管理。
- **gcloud 账号名称不等于 Colab OAuth 凭据。** ADC 文件未明确具有 Colab 所需权限时，不会自动用于切换或 usage 查询。
- **不切换虚拟机内部的认证。** Colab VM 中的 `google.colab.auth` / `colab auth` 与 CLI 控制面的 OAuth 登录是两套独立认证。
- **不停止或迁移已有运行时。** 切换前应查看并按需停止旧账号的 `colab sessions`。切换不会把旧账号的 VM 转移到新账号。
- `current` 不访问网络；`list` 只在线验证新发现的凭据；usage 始终需要网络。
- 邮箱用于防止误切账号，不是授权边界。令牌安全仍取决于操作系统钥匙串和本机账户安全。

## 存储与故障恢复

| 内容 | 位置 |
| --- | --- |
| Colab CLI 当前令牌 | 系统钥匙串：service `google-colab-cli`，username `oauth_token` |
| 旧版 Colab CLI 令牌（只读回退） | `~/.config/colab-cli/token.json` |
| 各 profile 令牌 | 系统钥匙串：service `colab-account-changer`，username 为 profile 别名 |
| 非敏感元数据 | `~/.config/colab-account-changer/config.json` |
| 并发锁 | 同目录的 `config.lock` |

默认配置目录权限为 `0700`，配置文件权限为 `0600`（POSIX）。新建目录会设为 `0700`，但不会修改自定义配置路径已有父目录的权限。配置在临时文件权限设置完成后原子替换，替换成功后不再执行可能造成“已提交却报错”的操作。

钥匙串读取有超时保护；写入和删除会同步等待后端完成，避免超时线程在回滚后延迟写入。若系统钥匙串被锁定，需要先解锁。

可以通过环境变量隔离配置和锁：

```bash
export COLAB_ACCOUNT_CHANGER_CONFIG=/path/to/config.json
```

usage 使用指定 profile 的 OAuth 凭据直接发送只读请求，不经过账号切换，也不创建运行时。profile 凭据缺失时会明确报错，不会回退到可能属于其他账号的当前令牌。网络错误不会直接展示可能包含令牌的请求 URL 或响应内容。

## 开发

```bash
uv sync
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
```

测试使用内存/临时目录中的伪造凭据。端到端测试使用临时文件钥匙串、隔离的 `HOME` 和伪造的 `colab` 可执行文件，不读取或修改真实 Google 账号、系统钥匙串或 Colab 配置。

## 许可证

[MIT](LICENSE)

# Colab Account Changer

基于 **Python + uv + Typer + Rich** 的 Google Colab CLI 多账号切换器。

它适配 [google-colab-cli](https://github.com/googlecolab/google-colab-cli) 的默认 `oauth2` 认证方式：当前令牌位于操作系统钥匙串的 `google-colab-cli` / `oauth_token` 条目中。切换器为每个账号在独立钥匙串条目中保存副本，本地配置文件只记录别名、邮箱、时间和 SHA-256 摘要，**不记录令牌内容**。

## 功能

- `save`：命名并保存当前 Colab 登录，默认在线验证 Google 身份。
- `add`：保存当前账号后调用 Colab CLI 登录新账号；失败或 Ctrl-C 时恢复。
- `use`：自动保存当前账号并切换；写入后回读校验，失败时回滚。
- `list`：用 Rich 表格展示账号、邮箱、活动状态和凭据状态。
- `current`：查看当前本地账号，不访问网络。
- `remove`：确认后删除非活动账号。
- 使用文件锁防止并发切换，配置采用原子写入和私有文件权限。

## 环境要求

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- 新版 `google-colab-cli`：支持钥匙串 OAuth 凭据和 `colab whoami`
- 可用的系统钥匙串（macOS Keychain、Windows Credential Locker 或 Linux Secret Service）

> 旧版 `~/.config/colab-cli/token.json` 不会被此工具删除或覆盖。如果检测到该文件，`add` 会先停止，避免 Colab CLI 在登录新账号时意外迁移旧账号。请先通过新版 Colab CLI 完成迁移。

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

### 1. 保存当前账号

如果 Colab CLI 尚未登录，先执行一个会触发 OAuth 登录的只读命令：

```bash
colab --auth=oauth2 sessions
```

然后将当前登录命名为 `personal`：

```bash
colab-account save personal
```

默认会刷新过期令牌并通过 Google `tokeninfo` 获取邮箱。无网络时可以仅保存本地凭据：

```bash
colab-account save personal --offline --email you@example.com
```

`--offline` 会跳过刷新和在线身份检查，因此邮箱只是人工标签，不代表已验证身份。

### 2. 添加另一个账号

```bash
colab-account add work
```

该命令会：

1. 将当前账号的最新凭据保存到其活动 profile。
2. 暂时清空 Colab CLI 的当前 OAuth 条目（已保存的 profile 不受影响）。
3. 执行 `colab --auth=oauth2 whoami`，由 Colab CLI 完成浏览器授权。
4. 将新凭据保存为 `work` 并设为活动账号。
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

切换前默认重新保存当前 profile，以保留 Colab CLI 刷新过的令牌。可用 `--offline` 跳过网络请求：

```bash
colab-account use work --offline
```

对已跟踪的当前账号，可使用 `--no-save` 保留其上次保存的副本。如果当前凭据尚未命名，切换器会拒绝覆盖；只有明确确认它可丢弃时才使用：

```bash
colab-account use work --discard-unsaved
```

**不要随意使用 `--discard-unsaved`。** 如果当前凭据没有副本，被替换后可能无法恢复。

### 4. 删除账号

```bash
colab-account remove work
# 跳过交互确认
colab-account remove work --yes
```

活动 profile 不能直接删除，请先切换账号。删除操作不会登出或清除 Colab CLI 当前凭据。

### 5. 覆盖已有 profile

如果 `save` 检测到已有 profile 的邮箱与当前已验证身份不同，会拒绝覆盖。确认需要替换时：

```bash
colab-account save work --force
```

## 重要边界

- **只切换 CLI 的默认 OAuth2 凭据。** `colab --auth=adc` 使用的 Application Default Credentials、gcloud 账号和 `GOOGLE_APPLICATION_CREDENTIALS` 不受此工具管理。
- **不切换虚拟机内部的认证。** Colab VM 中的 `google.colab.auth` / `colab auth` 与 CLI 控制面的 OAuth 登录是两套独立认证。
- **不停止或迁移已有运行时。** 切换前应查看并按需停止旧账号的 `colab sessions`。切换不会把旧账号的 VM 转移到新账号。
- `current` 和 `list` 展示本地状态；`save`、`add`、默认的 `use` 才在线验证身份。
- 邮箱用于防止误切账号，不是授权边界。令牌安全仍取决于操作系统钥匙串和本机账户安全。

## 存储与故障恢复

| 内容 | 位置 |
| --- | --- |
| Colab CLI 当前令牌 | 系统钥匙串：service `google-colab-cli`，username `oauth_token` |
| 各 profile 令牌 | 系统钥匙串：service `colab-account-changer`，username 为 profile 别名 |
| 非敏感元数据 | `~/.config/colab-account-changer/config.json` |
| 并发锁 | 同目录的 `config.lock` |

配置目录权限为 `0700`，配置文件权限为 `0600`（POSIX）。可以通过环境变量隔离配置和锁：

```bash
export COLAB_ACCOUNT_CHANGER_CONFIG=/path/to/config.json
```

切换器不会打印访问令牌、刷新令牌或 OAuth client secret。网络错误也不会直接展示可能包含令牌的请求 URL。

## 开发

```bash
uv sync
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
```

测试使用内存/临时目录中的伪造凭据。端到端测试还使用临时文件钥匙串和伪造的 `colab` 可执行文件，不读取或修改真实 Google 账号、系统钥匙串或 Colab 配置。

## 许可证

[MIT](LICENSE)

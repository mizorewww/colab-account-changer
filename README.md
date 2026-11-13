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
- 切换账号不会停止、创建或迁移已有 Colab VM。
- 切换前如需清理运行时，请自行使用 `colab sessions` 和 `colab stop`。

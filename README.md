# Colab Account Changer

**中文 | [English](README.en.md)**

Google Colab CLI 多账号管理：切换账号、查看计算单元余额、跨账号按名称管理会话。

## 安装

需要 Python 3.12+ 和 [uv](https://docs.astral.sh/uv/)：

```bash
uv tool install "google-colab-cli>=0.8.0"
uv tool install .
```

## 账号

```bash
colab-account list --usage   # 导入已有账号并刷新余额
colab-account add            # 登录新账号（登录链接自动复制到剪贴板），别名默认取邮箱名
colab-account add work       # 指定别名
colab-account use work       # 切换账号
colab-account current        # 当前账号
colab-account usage work     # 查看某账号余额
colab-account remove work    # 删除非活动账号
```

## 资源池

`colab-pool`（等同 `colab-account pool`）按名称管理会话，自动切换到会话所属账号。

```bash
colab-pool new node-x --gpu g4              # 在主账号上新建会话
colab-pool new --if-missing node-x --gpu g4 # 已存在则跳过
colab-pool exec node-x -f train.py          # 执行脚本
colab-pool upload node-x data.tgz /content/data.tgz
colab-pool list                             # 各账号余额与服务端实例
colab-pool stop node-x                      # 停止并注销
colab-pool stop-all                         # 停止所有账号上的实例
colab-pool reconcile                        # 检查未登记/已消失的会话
colab-pool primary work --threshold 3       # 设置主账号与切换阈值
colab-pool limits --max-per-account 6       # 会话数量上限
colab-pool disable old --reason "quota"     # 禁用账号
```

更多选项见 `--help`。详细行为说明见 [Wiki](https://github.com/mizorewww/colab-account-changer/wiki)；给 AI agent 用的操作规则见 [skills/colab-pool](skills/colab-pool/SKILL.md)。

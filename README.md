# Colab Account Changer

一个基于 Rich 的 Google Colab CLI 多账号切换器。完整安装与使用说明将在首个可运行版本中补充。

- 支持 [google-colab-cli](https://github.com/googlecolab/google-colab-cli) 的默认 OAuth2 凭据。
- 令牌仅保存在操作系统钥匙串中，不写入项目或普通配置文件。
- 切换前保存当前账号，切换失败时恢复原凭据。

> 开发中：命令接口以 `uv run colab-account --help` 为准。

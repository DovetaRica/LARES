# LARES

**Local AI Reasoning for Exceptional States**

**当前版本：v0.2.1，仅做只读观察与人工审核建议。**

新版提取了通用网页界面、分层记忆、时段/日报/周报流程、证据学习、模型对照和流式统计代码。家庭实体、原始事件、密钥、数据库、部署脚本和内部运维文档不进入公开包。

本次补丁修复报告覆盖与预算，增加无正文的模型调用统计、可恢复的进出方向确认，并采用 Rhine 界面。见 [兼容性与验证范围](docs/release-v021.md)。

## 离线体验

推荐 Python 3.12，在独立虚拟环境中运行：

```sh
python -m pip install ".[app]"
python -m app.demo
lares-app --demo
```

浏览器打开 http://127.0.0.1:8080 。演示使用临时合成数据，不接 HA、不调用模型、不控制设备。退出后临时文件删除。AI 报告不会被伪造为演示结果。

旧版 `python -m home_ai demo` 保持兼容，无需应用扩展依赖。

## 使用边界

应用默认关闭联网和定时任务。它会读取专用工作目录的 `.env`，因此不要从生产目录启动，也不要连接已有生产数据库。真实接入须显式配置网络开关、HTTPS 远程端点和自己的实体白名单。模型、提示词效果、完整真实接入及跨版本数据库迁移尚未验证。

来源项目的 466 项测试结果不代表公开提取版的结果；请以本版独立验证为准。已有预处理性能限制仍保留，不承诺 GPU 性能、识别准确率或生产吞吐。

[新版安装与边界](docs/application.md) · [提取说明](docs/extraction-v02.md) · [原 CLI 部署](docs/deployment.md) · [版本档案](docs/versioning.md) · [隐私](docs/privacy.md)

每版保留独立本地 source、artifacts、reviews 和 SUGGESTIONS；不会将私有审查材料推送到 GitHub。此次提取不改变现有服务。

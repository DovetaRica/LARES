# LARES

**Local AI Reasoning for Exceptional States**

**默认确定性，只在需要时引入智能。**

**自动化处理常规事件，AI 处理特殊情况。**

为 Home Assistant 提供旁路异常观察与解释能力。现有自动化独立运行；重复的人工纠正可以形成待审核案例。

当前版本：`v0.1.0-alpha.3`，仅支持观察，不执行设备动作。

## 立即体验

需要 Python 3.11 或更新版本。在仓库目录执行：

```sh
python -m home_ai demo
python -m home_ai doctor --json
python -m unittest discover -s tests -v
```

无需安装依赖即可运行离线 Demo。输出为 JSON，包含三个合成场景：机器人活动与人员存在、传感器冲突、反复人工纠正。默认提供者 `fixture` 是固定测试响应，不是真实大模型推理。

## 已实现

- 事件数、时间跨度、完整模型请求大小限制（包括系统提示和 JSON 封装）；重复过滤和调用冷却。
- 从原项目提取的 SQLite 存储、环境统计聚合、异常检测与候选记忆，用于离线回放。
- 回放中重复三次人工纠正，生成待审核候选；不生成或启用自动化。
- 显式开启的 HA 事件订阅和 Ollama 推理入口。
- 模型失败、非法输出、伪造证据时放弃判断。
- 独立 Docker 示例、配置校验、诊断、测试和 Agent 文档。

实时观察已加入有界接收队列、畸形消息容错、队列溢出与过期计数，目前仍仅输出判断，未接入长期数据库、模式学习及断线自动重连。上下文采用截断，层级摘要尚未迁移。数据库中候选分数属于启发式内部指标，不是模型准确率。

## 使用边界

公共版本默认不读取其他目录的配置，不自动加载 `.env`，不启动定时任务。真实 HA 接入要求专门的 token 环境变量和实体映射；模型调用要求 `--enable-model`。详见 [部署说明](docs/deployment.md)。

Arc A310 是后续实测目标，当前没有发布硬件性能或兼容性结论。容器配方尚未运行验证，当前验证基线为 Python 命令。

[架构](docs/architecture.md) · [隐私](docs/privacy.md) · [提取记录](docs/extraction.md) · [验收](docs/validation.md) · [发布清单](docs/release.md)

本地每个发行版本分别保存代码快照、逐轮审查和建议，详见 [版本档案管理](docs/versioning.md)。

本版修复记录见 [alpha.2 审查整改](docs/review-fixes-alpha2.md)。wheel 已包含示例，安装后可在任意工作目录运行 `home-ai demo`。HA 与模型的非本机端点都需要各自的远程开关和 HTTPS；不再接受远程明文 HTTP。

项目仓库：[DovetaRica/LARES](https://github.com/DovetaRica/LARES)。Python 发行包名 `home-ai-exceptions`、模块 `home_ai`、命令 `home-ai` 保持兼容。

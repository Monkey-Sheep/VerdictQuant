# VerdictQuant — AI K线研究与模拟量化工作台

---

VerdictQuant 面向主观研究者和策略验证者。从 **MT5 / TradingView / yfinance / AkShare** 读取 K 线，将结构化数据与预计算特征送入大模型做**两阶段分析**（市场诊断 → 交易决策），并把信号送入本地模拟账户验证。它**不是**截图识图，**不连接券商、不执行真实下单**。

> VerdictQuant 是基于 PA Agent 修改形成的独立衍生项目，首次公开修改日期为
> 2026-07-13。项目整体继续遵循 AGPL-3.0-or-later；上游版权、修改说明和
> 第三方许可证见 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) 与
> [`UPSTREAM_INTEGRATION.md`](UPSTREAM_INTEGRATION.md)。这些内容仅用于许可证合规，
> 不代表上游作者对本项目的背书。

> Windows `1.0.0` 加固发行版用户请先阅读
> [`USER_GUIDE_CN.md`](USER_GUIDE_CN.md)。它覆盖首次启动、模型配置、
> PA 分析、自动量化模拟、账户维护、AI/CLI 调用和故障排查。

---

## 主要功能

- 📈 **多数据源**：MT5（Windows）、TradingView（全平台）、yfinance（期货/加密货币）、AkShare（A 股）
- 🧠 **两阶段 AI 分析**：市场诊断 → 策略路由 → 交易决策（限价/突破/市价或不下单）
- 🔄 **增量分析与持续跟踪**：新增 K 线时复用上次结论；开启 `keep_analysis` 后新 K 线收盘自动触发新一轮分析
- 🌳 **决策树可视化**：赛博科幻风格可交互流程图，自动播放闸门→策略路径动画
- 🔮 **未来走势预期**：AI 预测下一根 K 线方向和下一个市场周期位置
- 💬 **分析后自由追问**：完整对话会话管理器，实时推理流 + Token 进度条，对话历史持久化
- 📚 **经验库**：按周期位置检索历史案例供分析参考
- 📝 **完整落盘**：Prompt、原始响应、诊断/决策 JSON、Token 用量、追问记录
- 🛡️ **可配置校验体系**：JSON 校验、一致性检查、语义校验、截断修复、失败自动重试
- 🔒 **API Key** 本地加密存储

---

## 环境要求

| 项目     | 要求                                                                    |
| -------- | ----------------------------------------------------------------------- |
| 操作系统 | Windows 10 / 11（主支持）、macOS 12+（TradingView 数据源）              |
| Python   | 3.11+                                                                    |
| 数据源   | MT5 / TradingView / yfinance / AkShare **至少配置一种**                  |
| 网络     | 可访问所配置的 AI API（如 DeepSeek、PackyAPI 等）                        |

---

## 快速开始

直接在系统中安装（推荐部署在本机）：

```cmd
pip install -e .
python -m pa_agent.main
```

首次启动后在**设置**中填写 **Base URL**、**模型名** 与 **API Key**。

> 如需隔离环境也可创建虚拟环境：`python -m venv .venv` 后激活再 `pip install -e .`。

**安装内容**：PyQt6（GUI 框架）+ pyqtgraph（K 线图表绘图）+ numpy/pandas（数据处理）+ openai（AI API 客户端）+ **akshare/baostock（A 股数据源）** + json 校验、模型定义等全套依赖。

> 若需运行测试（pytest）或代码格式化（ruff/black），额外安装：`pip install -e ".[dev]"`。

---

## 详细说明

未发布变更：新增“综合研究中心 → 组合监控”。打开只读上次结果，点击刷新才获取公开数据；支持 `VerdictQuant.exe --monitor` 直接查看，无定时推送、账户连接或下单。使用与数据限制见 [手动监控说明](docs/MANUAL_MONITOR_CN.md)。

`1.0.0` 完整教程见 [`USER_GUIDE_CN.md`](USER_GUIDE_CN.md)，配置字段说明见
[`config/README.md`](config/README.md)。

---

**免责声明**：本工具仅供学习与研究，不构成投资建议。交易有风险，决策后果自负。

本项目采用 [GNU Affero General Public License v3.0 (AGPL-3.0)](LICENSE) 发布。

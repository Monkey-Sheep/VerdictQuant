# 在 VerdictQuant 中手动查看组合监控

股票分批投入分析见[分批投资研究说明](INSTALLMENT_RESEARCH_CN.md)。本页说明基金与行情监控。用户选择软件手动查看后，对话定时监控保持暂停，不因启动软件自动恢复；各版本的实际交付状态以交付回执为准。

## 使用

1. 打开桌面“VerdictQuant 组合监控”，在左侧选择“基金与行情”。原综合研究中心中的组合监控入口及 `VerdictQuant.exe --monitor` 也可以进入同一工作台。
2. 打开时显示上次保存结果。点击“刷新行情”才请求公开数据；刷新中不能重复提交，失败保留旧结果。
3. 上方先看 001437 正式净值与核心 ETF；点击卡片“详情”或行情表中的标的，右侧查看日期、变化与数据状态。“指标与数据依据”和“监控边界与研究缺口”可以展开。

2026-09-08 界面改版：股票研究与基金行情使用统一侧栏和浅色界面。左侧“投资计划”设置预算与持仓；“股票研究”先展示本期判断与价格走势，完整依据、财务、来源和记录分别查看。改版不改变监控规则、投资判断或真实资金权限。

监控窗口独立启动不会加载模型密钥或银行、券商、钱包账户。它不执行订单，也不在后台定时推送。

基金和美股分别展示完成日期、日/五交易日变化、均线、回撤及数据缺口。核心 ETF 的规则只标记是否需要人工复核，不能替代对新闻、估值、广度及最新披露的判断。公告/经理、最新披露持仓和完整前瞻尚未自动核验时，界面明确保留这些缺口，不生成买价、提前仓或减仓数量。

## 本地资料

- 当前机器的公开结果默认在 `D:/CodexData/finance/monitoring/desktop`，可通过 `VERDICTQUANT_MONITOR_ROOT` 指定其他目录。
- 每次刷新新建一个结果目录。`current.json` 只指向校验过的完整 JSON 文件；此前结果仍保留。软件打开不会自动创建新结果或刷新市场数据。
- `paths.json` 可指定 `workspace` 和只读公共净值缓存 `fund_db`，只保存本机路径，不保存账户连接信息。
- `workspace/portfolio_state.json` 仅接受结构化、可追溯的用户确认记录。旧 Markdown 正文不能证明已确认持仓或已执行动作。缺失保持未知，程序不创建或猜测真实持仓。
- `policy.json` 是唯一监控范围与核心价格门槛输入。BTC仅作背景；观察池与真实持仓分别判断。

结构化状态格式示例（没有真实持仓，不要将示例视为用户确认）：

```json
{
  "schema_version": 1,
  "positions": {"001437": {"status": "unknown"}},
  "executions": [],
  "execution_history_complete": false
}
```

确认持仓需要 `status=confirmed`、非负数值 `quantity`、带时区的 `confirmed_at`、可追溯的 `source_ref`；正持仓的完整资料另需 `average_cost` 和 `purchase_date`。可以用 `review_after` 明确确认记录的复核期限。零持仓与未知不同。

执行记录单独保存 `event_id/proposal_id/symbol/action/quantity/executed_at/source_ref`。每条只能是实际确认的 ADD、REDUCE 或 EXIT；重复事件被拒绝，同一提议已执行后不能再次作为未执行减仓。监控程序不自动写执行记录。

## 日历和验证

当前官方日历覆盖 2026 年。超出覆盖、时区未知或历史样本不足会明确失败，不猜测未来交易日。

- 美国：[NYSE 休市与提前收市表](https://www.nyse.com/trade/hours-calendars)。
- 中国：[上交所 2026 休市安排](https://www.sse.com.cn/disclosure/dealinstruc/closed/)。调休周末不当作交易日。
- 韩国：[KRX 年度休市表](https://open.krx.co.kr/contents/MKD/01/0110/01100305/MKD01100305.jsp)，2026 年数据通过其页面原生公开查询核对，共 17 个休市日期。
- 时区使用 Python `zoneinfo` 和 IANA/tzdata，保留夏令时；FX 按数据源的伦敦会话日期解释。

离线检查：

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests/unit -p test_manual_monitor.py -v
```

覆盖状态否定/缺失/零持仓、已执行动作去重、节假日/夏令时/提前收市、指标不足、保存完整性、刷新失败保留旧结果、跨窗口刷新锁，以及实际 Qt 点击和重复点击行为。测试不访问真实账户或联网获取市场数据。

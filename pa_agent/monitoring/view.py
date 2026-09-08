"""Escaped desktop report content. Unknown is never rendered as zero or normal."""
from __future__ import annotations

from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo


def display(value, percent=False, digits=2):
    if value is None:
        return "待核验"
    return f"{value:,.{digits}f}" + ("%" if percent else "")


def render(result: dict | None) -> str:
    if result is None:
        return "<h2>还没有监控结果</h2><p>点击右上方“刷新监控”获取公开行情。以后打开此页会直接显示上次结果。</p><p>不会自动刷新、推送消息或执行交易。</p>"
    data = result["data"]
    assets = data["assets"]
    checked = datetime.fromisoformat(result["checked_at"]).astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")
    state_known = result["state"]["status"] == "VALID"
    parts = [f"<p>上次刷新：{checked}（北京时间） · 仅手动刷新</p>",
             "<p><b>行情指标已读取；完整研究仍有待核验项。</b> 下方价格日期与刷新时间分别显示。</p>"]
    if not state_known:
        parts.append("<p style='color:#e9b949'>持仓与已执行动作尚未确认，不生成买卖数量，也不会将旧建议视为已执行。</p>")
    fund = assets.get("001437")
    parts.append("<h2>基金 · 易方达瑞享混合I</h2>")
    if fund:
        ma = fund["ma"]
        weak = fund.get("qualified") and ma["20"] is not None and ma["50"] is not None and fund["close"] < min(ma["20"], ma["50"])
        posture = "短期价格偏弱，仅作走势背景" if weak else "趋势数据待复核" if not fund.get("qualified") else "持仓保持观察；不以均线直接决定增减"
        parts += [f"<p><span style='font-size:26px'><b>{display(fund['close'], digits=4)}</b></span>　正式净值日期：{escape(fund['date'])}</p>",
                  f"<p>{posture}　日变化 {display(fund.get('daily_pct'), True)}　五交易日 {display(fund.get('five_session_pct'), True)}</p>",
                  f"<p>20 / 50 / 200 日均线：{display(ma['20'])} / {display(ma['50'])} / {display(ma['200'])}</p>",
                  f"<p>距52周高点：{display(fund['drawdown_52week_pct'], True)}　200日均线斜率：{display(fund.get('ma200_twenty_session_slope_pct'), True)}</p>",
                  "<p>当前安排为保留已有基金持仓。这里只显示公开行情，不生成自动申购、赎回或减仓指令。</p>"]
    else:
        parts.append("<p>基金数据获取失败，未使用旧净值冒充本次结果。</p>")
    parts.append("<h2>美股 · 核心与观察池</h2>")
    parts.append("<table cellspacing='0' cellpadding='7' width='100%'><tr><th align='left'>标的</th><th align='left'>完成日期</th><th align='right'>收盘</th><th align='right'>日变化</th><th align='right'>五交易日</th><th align='right'>新报价</th><th align='left'>状态</th></tr>")
    symbols = result["universe"]
    for symbol in symbols:
        asset = assets.get(symbol)
        if not asset:
            parts.append(f"<tr><td>{symbol}</td><td colspan='6'>获取失败，保留未知</td></tr>")
            continue
        check = result["core_checks"].get(symbol, {})
        state = "数据待核验" if not asset.get("qualified") else "仅观察，不生成买价"
        if check.get("status") == "PRICE_GATE_REACHED":
            state = "触及价格复核线，需人工结合其他证据"
        elif check.get("status") == "PRICE_GATE_NOT_REACHED":
            state = "未触及价格复核线；核心持仓仅观察"
        quote = asset.get("quote") or {}
        live = display(quote.get("price")) if quote.get("fresh") else "—"
        parts.append(f"<tr><td><b>{symbol}</b></td><td>{escape(asset['date'])}</td><td align='right'>{display(asset['close'])}</td><td align='right'>{display(asset.get('daily_pct'), True)}</td><td align='right'>{display(asset.get('five_session_pct'), True)}</td><td align='right'>{live}</td><td>{state}</td></tr>")
    parts.append("</table><p>新报价仅在来源时间足够新时显示，不计入未完成日线。价格复核线不等于减仓指令；股票投入研究请切换“分批投资研究”。BTC仅作COIN背景。</p>")
    for symbol in result["core_checks"]:
        asset = assets.get(symbol)
        if asset:
            ma = asset["ma"]
            parts.append(f"<p><b>{escape(symbol)}</b>　20/50/200日均线 {display(ma['20'])} / {display(ma['50'])} / {display(ma['200'])}　距52周高点 {display(asset['drawdown_52week_pct'], True)}　200日斜率 {display(asset.get('ma200_twenty_session_slope_pct'), True)}</p>")
    parts.append("<h3>数据与研究缺口</h3><ul>")
    labels = {"STALE_COMPLETED_HISTORY": "最近完成交易日数据缺失", "INSUFFICIENT_HISTORY": "历史样本不足", "INSUFFICIENT_NAV_HISTORY": "净值历史样本不足", "PENDING_PUBLICATION": "当日正式净值尚待披露", "STALE": "正式净值尚未更新", "SAVED_DATA_NEEDS_REFRESH": "保存的数据需要手动刷新"}
    labels.update({"EMPTY_OR_DUPLICATE_DATES": "来源日线为空或包含重复日期", "HIGH_BELOW_CLOSE": "来源高价与收盘价口径不一致", "INVALID_PRICE": "来源价格无效"})
    for symbol, errors in sorted(data.get("errors", {}).items()):
        descriptions = [labels.get(error, "来源或交易日尚未通过校验") for error in errors]
        parts.append(f"<li>{escape(symbol)}：{escape('；'.join(descriptions))}</li>")
    parts += [f"<li>{escape(gap)}</li>" for gap in result.get("research_gaps", [])]
    parts.append("</ul><p>未来5–20个交易日前瞻、潜在底部和置信度尚未完成研究，不以技术评分替代上涨概率。当前不生成提前仓或确认仓数量。</p>")
    parts.append("<p><a href='https://www.efunds.com.cn/fund/001437.shtml'>基金正式资料</a>　<a href='https://www.nyse.com/trade/hours-calendars'>美股交易日</a></p>")
    return "".join(parts)

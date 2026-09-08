"""Validate model judgments against public evidence; compute budgets locally.

Price rank is descriptive. It is intentionally NOT a prerequisite for starting
installments, nor does a drawdown ever mechanically increase an allocation.
"""
from __future__ import annotations

import math
import re
from datetime import UTC, date, datetime, timedelta

from .models import CYCLICAL, HIGH_UNCERTAINTY, NAMES, SYMBOLS, THEMES, current_period, finite
from .metrics import metric_value, simplify_financials, verified_facts

LABELS = {"START": "可开始小额分批", "NORMAL": "可按正常节奏投入", "INCREASE": "价格更有吸引力",
          "PAUSE": "本期暂缓新增", "REVIEW": "本次证据不足"}
DECISION_VERSION = "installment-policy-20260908.3"
MULTIPLES = {"pe_ttm": "pe", "normalized_pe": "pe", "ps_ttm": "ps", "ev_sales": "ev_revenue"}


def _texts(value, maximum=6):
    if not isinstance(value, list) or len(value) > maximum or any(not isinstance(x, str) or len(x) > 1800 for x in value):
        raise ValueError("MODEL_TEXT_INVALID")
    return value


def validate_judgment(value: dict, symbol: str, source_ids: set[str]) -> dict:
    if not isinstance(value, dict) or value.get("symbol") != symbol:
        raise ValueError("MODEL_SYMBOL_MISMATCH")
    if value.get("business_state") not in {"intact", "uncertain", "impaired"}:
        raise ValueError("MODEL_BUSINESS_STATE_INVALID")
    if value.get("valuation_method") not in {*MULTIPLES, "unknown"}:
        raise ValueError("MODEL_VALUATION_METHOD_INVALID")
    if value.get("confidence") not in {"low", "medium", "high"}:
        raise ValueError("MODEL_CONFIDENCE_INVALID")
    for key in ("summary", "valuation_explanation"):
        if not isinstance(value.get(key), str) or not 1 <= len(value[key]) <= 2200:
            raise ValueError("MODEL_EXPLANATION_INVALID")
    for key in ("reasons", "risks", "assumptions", "invalidators"):
        _texts(value.get(key), 8)
    if not value["risks"] or not value["invalidators"]:
        raise ValueError("MODEL_RISK_OR_REVIEW_MISSING")
    for text in [value["summary"], value["valuation_explanation"], *value["reasons"], *value["risks"], *value["invalidators"]]:
        if re.search(r"\d|[零一二三四五六七八九十百千万亿点]+(?:亿|百万|万美元|百分之|倍市盈率)", text):
            raise ValueError("MODEL_UNVERIFIED_NUMERIC_NARRATIVE")
    for key in ("business_evidence", "valuation_evidence", "negative_evidence"):
        ids = _texts(value.get(key, []), 15)
        if set(ids) - source_ids:
            raise ValueError("MODEL_UNKNOWN_SOURCE")
    if not value["business_evidence"]:
        raise ValueError("MODEL_BUSINESS_EVIDENCE_MISSING")
    low = finite(value.get("fair_multiple_low"), minimum=.05, maximum=1000, optional=True)
    high = finite(value.get("fair_multiple_high"), minimum=.05, maximum=1000, optional=True)
    if value["valuation_method"] != "unknown" and (low is None or high is None or low >= high or not value["assumptions"] or not value["valuation_evidence"]):
        raise ValueError("MODEL_VALUATION_ASSUMPTIONS_MISSING")
    if value["valuation_method"] == "normalized_pe":
        finite(value.get("normalized_profit_fraction"), minimum=.1, maximum=1)
    days = value.get("review_in_days", 7)
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 30:
        raise ValueError("MODEL_REVIEW_DATE_INVALID")
    return value


def _nodes(value):
    if isinstance(value, dict):
        if "value" in value:
            yield value
        for item in value.values():
            yield from _nodes(item)
    elif isinstance(value, list):
        for item in value:
            yield from _nodes(item)


def _recent_financials(research: dict, now: datetime, usable: set[str]) -> bool:
    available = set()
    for section in (research.get("financials", {}), research.get("vendor_financials", {})):
        if section.get("stale"):
            continue
        for metric in ("revenue", "net_income", "operating_cash_flow"):
            for item in _nodes(section.get("metrics", {}).get(metric, {})):
                if metric_value(item) is None:
                    continue
                if item.get("stale") or not item.get("source_refs") or set(item["source_refs"]) - usable:
                    continue
                end = item.get("period_end") or item.get("as_of") or item.get("asOfDate")
                try:
                    if 0 <= (now.date() - date.fromisoformat(str(end)[:10])).days <= 180:
                        available.add(metric)
                except ValueError:
                    pass
    return "revenue" in available and bool(available & {"net_income", "operating_cash_flow"})


def _multiple(research: dict, method: str, price: dict, now: datetime, usable: set[str]):
    metrics = research.get("valuation", {}).get("metrics", {})
    node = dict(metrics.get(MULTIPLES.get(method, "")) or {})
    value = metric_value(node)
    if research.get("valuation", {}).get("stale"):
        return None, node, ["估值来源本次获取失败，仅有旧缓存。"]
    if value is None or value <= 0:
        return None, node, ["所选估值指标不可用；亏损公司的负市盈率不作便宜依据。"]
    if not node.get("source_refs") or set(node["source_refs"]) - usable:
        return None, node, ["估值指标未绑定本次有效来源。"]
    stamp = node.get("as_of") or node.get("period_end")
    try:
        age = (now.date() - date.fromisoformat(str(stamp)[:10])).days
    except ValueError:
        age = 9999
    if not 0 <= age <= 35:
        return None, node, ["估值指标日期过旧或异常。"]
    # Rebase vendor valuation to the same latest completed close, if the
    # historical close at the valuation observation is available.
    candidates = [r for r in price.get("rows", []) if r["date"] <= stamp[:10]]
    if not candidates or (date.fromisoformat(stamp[:10]) - date.fromisoformat(candidates[-1]["date"])).days > 4:
        return None, node, ["估值观测日与股票价格无法对齐，暂不计算买入判断。"]
    reference_price = candidates[-1]["close"]
    if method == "ev_sales":
        mc, ev = metrics.get("market_cap") or {}, metrics.get("enterprise_value") or {}
        if (not metric_value(mc) or not metric_value(ev) or mc.get("unit") != "USD" or ev.get("unit") != "USD"
                or mc.get("as_of") != stamp or ev.get("as_of") != stamp
                or not mc.get("source_refs") or not ev.get("source_refs")
                or set(mc["source_refs"] + ev["source_refs"]) - usable):
            return None, node, ["企业价值不能按股价同比缩放；缺同日同币种市值和净债务，需改用可核验方法。"]
        bridge = {"market_cap": mc["value"], "enterprise_value": ev["value"],
                  "net_debt": ev["value"] - mc["value"], "reference_price": reference_price,
                  "reference_multiple": value}
        updated_ev = mc["value"] * price["close"] / reference_price + bridge["net_debt"]
        if updated_ev <= 0:
            return None, node, ["企业价值非正，不能用此倍数推断合理价格。"]
        value *= updated_ev / ev["value"]
        node["equity_bridge"] = bridge
    else:
        value *= price["close"] / reference_price
    return value, node, []


def financial_conflicts(research: dict, usable: set[str]) -> list[str]:
    """Flag material comparable discrepancies, not FX or fiscal-calendar noise."""
    official = research.get("financials", {}).get("metrics", {})
    vendor = research.get("vendor_financials", {}).get("metrics", {})
    conflicts = []
    for metric in ("revenue", "net_income"):
        for period in ("annual", "latest_quarter", "ttm", "latest"):
            a = official.get(metric, {}).get(period) or {}
            b = vendor.get(metric, {}).get(period) or {}
            av, bv = metric_value(a), metric_value(b)
            if av is None or bv is None or a.get("unit") != b.get("unit") or a.get("stale") or b.get("stale"):
                continue
            if not a.get("source_refs") or not b.get("source_refs") or set(a["source_refs"] + b["source_refs"]) - usable:
                continue
            try:
                # Fiscal 52/53-week reporting and vendor calendar month-end
                # labels can differ by several days while covering one quarter.
                if abs((date.fromisoformat(a["period_end"]) - date.fromisoformat(b["period_end"])).days) > 8:
                    continue
            except (KeyError, TypeError, ValueError):
                continue
            if max(abs(av), abs(bv)) > 0 and abs(av - bv) / max(abs(av), abs(bv)) > .20:
                conflicts.append(f"{metric}/{period}：相近报告期、同币种的正式财报与供应商数字差异超过20%，需核实合并范围或会计口径。")
    return conflicts


def missing_assessment(symbol: str, price: dict, research: dict, reason: str, now: datetime):
    return {"symbol": symbol, "name": NAMES[symbol], "decision": "REVIEW", "decision_label": LABELS["REVIEW"],
            "confidence": "low", "summary": "本次尚不能形成有依据的新判断；这不等于建议停止你原有的定投。",
            "reasons": [reason], "risks": ["缺失资料不能视为低风险或已排除重大事件。"],
            "next_review": (now + timedelta(days=1)).date().isoformat(),
            "review_at": (now + timedelta(days=1)).isoformat(),
            "invalidators": ["补齐本条缺口后，点击更新分析重新判断。"], "sources": research.get("sources", []),
            "price": price, "valuation": {"status": "unknown", "label": "估值待核验", "method": "unknown", "explanation": reason, "metrics": {}},
            "fundamentals": simplify_financials(research), "errors": [reason],
            "budget": {"amount_usd": None, "label": "未生成金额", "reason": "本次研究证据不足。"}}


def assess(symbol: str, price: dict, research: dict, judgment: dict, now: datetime) -> dict:
    sources = research.get("sources", [])
    usable = {x["id"] for x in sources if x.get("status", "ok").lower() in {"ok", "success", "cached"} and not x.get("stale")}
    judgment = validate_judgment(judgment, symbol, usable)
    if (research.get("identity") or {}).get("status") != "verified":
        return missing_assessment(symbol, price, research, "当前股票代码与发行人身份尚未核实。", now)
    if not price.get("qualified") or price.get("currency") != "USD":
        return missing_assessment(symbol, price, research, "当前发行人的最新完成交易日行情未通过校验。", now)
    financial_ids = {ref for section in (research.get("financials", {}), research.get("vendor_financials", {}))
                     for node in _nodes(section) for ref in node.get("source_refs", []) if ref in usable}
    document_ids = {ref for doc in research.get("documents", []) if doc.get("text") and not doc.get("stale")
                    for ref in doc.get("source_refs", []) if ref in usable}
    if not set(judgment["business_evidence"]) & (financial_ids | document_ids):
        return missing_assessment(symbol, price, research, "经营判断只引用了标题或索引，缺少可验证的财务或正文证据。", now)
    if not _recent_financials(research, now, usable):
        return missing_assessment(symbol, price, research, "没有近期、日期有效的经营财务数据，不能只凭价格或新闻标题给买入判断。", now)
    conflicts = financial_conflicts(research, usable)
    if conflicts:
        return missing_assessment(symbol, price, research, "；".join(conflicts), now)
    business = judgment["business_state"]
    method = judgment["valuation_method"]
    if method == "pe_ttm" and symbol in CYCLICAL:
        return missing_assessment(symbol, price, research, "周期性存储企业不能直接用高景气期静态市盈率判断便宜；需正常化假设。", now)
    multiple, multiple_node, warnings = _multiple(research, method, price, now, usable)
    if multiple is not None and not set(judgment["valuation_evidence"]) & set(multiple_node.get("source_refs", [])):
        return missing_assessment(symbol, price, research, "估值判断没有引用实际使用的有效估值来源。", now)
    if method == "normalized_pe" and multiple is not None:
        multiple /= judgment["normalized_profit_fraction"]
    low, high = judgment.get("fair_multiple_low"), judgment.get("fair_multiple_high")
    confidence = judgment["confidence"]
    if symbol in HIGH_UNCERTAINTY or method in {"ps_ttm", "ev_sales", "normalized_pe"}:
        confidence = "low" if judgment["confidence"] == "low" else "medium"
    if business == "impaired" and not set(judgment.get("negative_evidence", [])) & (financial_ids | document_ids):
        return missing_assessment(symbol, price, research, "负面新闻标题不足以证明经营理由被破坏，需核对财务或正式正文。", now)
    if business == "impaired":
        decision, label = "PAUSE", "经营理由需要重新检验"
    elif business != "intact" or multiple is None or low is None or high is None:
        decision, label = "REVIEW", "资料或关键经营判断不足"
    elif multiple > high:
        decision, label = "PAUSE", "超过所列假设的可接受估值区间"
    elif multiple < low and confidence != "low" and symbol not in HIGH_UNCERTAINTY:
        decision, label = "INCREASE", "低于模型所列情景区间，仍须检查预算"
    else:
        decision, label = "NORMAL", "处于所列假设可接受的投入区间"
    # A first installment is not a completed transaction. The budget layer
    # may label START only when the user has actually confirmed zero holdings.
    range_prices = ([round(price["close"] * low / multiple, 2), round(price["close"] * high / multiple, 2)]
                    if multiple and low and high else None)
    bridge = multiple_node.get("equity_bridge")
    if bridge and range_prices:
        range_prices = [round(max(0.0, (bound * bridge["enterprise_value"] / bridge["reference_multiple"] - bridge["net_debt"]) / bridge["market_cap"] * bridge["reference_price"]), 2)
                        for bound in (low, high)]
    assumptions = judgment["assumptions"]
    explanation = judgment["valuation_explanation"]
    summaries = {"NORMAL": "按本次明确列出的估值假设，当前价格可讨论正常分批投入，无需等待历史最低点。",
                 "INCREASE": "按本次估值假设，价格更有余地；是否增加本期投入还取决于你确认的预算与仓位。",
                 "PAUSE": "本次模型认为经营理由或价格不满足所列条件，建议暂缓新增；这不代表预测股价一定下跌。",
                 "REVIEW": "本次关键经营或估值判断仍不足，不能形成新的投入意见；这不等于建议停止原有计划。"}
    return {"symbol": symbol, "name": NAMES[symbol], "decision": decision, "decision_label": LABELS[decision],
            "confidence": confidence, "summary": summaries[decision],
            "reasons": [label, *verified_facts(research), *warnings], "risks": judgment["risks"],
            "invalidators": judgment["invalidators"], "next_review": (now + timedelta(days=judgment["review_in_days"])).date().isoformat(),
            "review_at": (now + timedelta(days=judgment["review_in_days"])).isoformat(),
            "sources": sources, "price": price, "fundamentals": simplify_financials(research),
            "valuation": {"status": "scenario" if multiple else "unknown", "label": label,
                          "method": method, "explanation": explanation, "metrics": research.get("valuation", {}).get("metrics", {}),
                          "observed_multiple": multiple_node.get("value"), "observed_at": multiple_node.get("as_of"),
                          "aligned_multiple": multiple, "fair_multiple_low": low, "fair_multiple_high": high,
                          "scenario_price_range": range_prices, "assumptions": assumptions,
                          "warning": "区间由 AI 情景假设推导，不是市场公认公允价值、预测底部或收益保证。"},
            "model_judgment": judgment, "errors": warnings,
            "budget": {"amount_usd": None, "label": "待核对投资计划", "reason": "未计算个人投入金额。"}}


def allocate(assessments: list[dict], plan: dict, contributions: list[dict], now: datetime):
    """Allocate ONLY user-confirmed USD funds. Unknown never becomes zero."""
    spent = {s: 0.0 for s in SYMBOLS}
    post_snapshot = {s: 0.0 for s in SYMBOLS}
    reconciliation_unknown = False
    snapshot_times = {}
    for symbol in SYMBOLS:
        try:
            stamp = datetime.fromisoformat(plan.get("position_confirmed_at", {}).get(symbol) or plan.get("confirmed_at") or "")
            if stamp.tzinfo is None:
                raise ValueError()
            snapshot_times[symbol] = stamp
        except (TypeError, ValueError):
            snapshot_times[symbol] = None
    for row in contributions:
        if row["period"] == current_period(now) and row["symbol"] in spent:
            spent[row["symbol"]] += row["amount_usd"]
        try:
            stamp = datetime.fromisoformat(row.get("confirmed_at") or "")
            if stamp.tzinfo is None or stamp > now + timedelta(seconds=60):
                raise ValueError()
            snapshot_at = snapshot_times.get(row["symbol"])
            if snapshot_at and stamp > snapshot_at and row["symbol"] in post_snapshot:
                post_snapshot[row["symbol"]] += row["amount_usd"]
        except (TypeError, ValueError):
            reconciliation_unknown = True
    missing = []
    if plan.get("period") != current_period(now):
        missing.append("计划月份需要重新确认")
    if not plan.get("monthly_budget_usd"):
        missing.append("本月预算未填写或为零")
    if plan.get("horizon_years") is None:
        missing.append("预计持有年限未填写")
    if not plan.get("portfolio_total_usd"):
        missing.append("全部投资资产市值未填写")
    try:
        if not 0 <= (now - datetime.fromisoformat(plan.get("confirmed_at") or "")).total_seconds() <= 35 * 86400:
            missing.append("投资计划需更新确认")
    except (ValueError, TypeError):
        missing.append("投资计划尚未确认")
    if reconciliation_unknown:
        missing.append("已投入记录与持仓快照的时间无法对账")
    total = plan.get("portfolio_total_usd") or 0
    monthly = plan.get("monthly_budget_usd") or 0
    remaining = max(0.0, monthly - sum(spent.values()))
    positions = {s: None if plan.get("positions_usd", {}).get(s) is None else plan["positions_usd"][s] + post_snapshot[s] for s in SYMBOLS}
    theme_used = {k: sum(positions.get(s) or 0 for s in members) for k, members in THEMES.items()}
    allocations, allocation_gaps = {}, {}
    for item in assessments:
        s = item["symbol"]
        base_decision = item.get("research_decision", "NORMAL" if item["decision"] == "START" else item["decision"])
        item["research_decision"] = base_decision
        item["decision"], item["decision_label"] = base_decision, LABELS[base_decision]
        gaps = list(missing)
        weight = plan.get("target_weights", {}).get(s, 0)
        if positions.get(s) is None:
            gaps.append("该股票持仓市值未知，不能当作零")
        elif snapshot_times[s] is None or not 0 <= (now - snapshot_times[s]).total_seconds() <= 35 * 86400:
            gaps.append("该股票持仓快照超过复核期限")
        relevant = [key for key, members in THEMES.items() if s in members]
        if any(any(positions.get(other) is None for other in THEMES[key]) for key in relevant):
            gaps.append("同主题股票持仓未全部确认")
        if not weight:
            gaps.append("未分配本月预算比例")
        if item["decision"] in {"REVIEW", "PAUSE"}:
            amount = None if item["decision"] == "REVIEW" else 0.0
            reason = "本次证据不足，原有计划是否继续需另行判断。" if amount is None else "本期研究建议暂缓新增；未发出卖出指令。"
        elif gaps:
            amount, reason = None, "；".join(gaps)
            if weight > 0:
                allocation_gaps[s] = gaps
        elif plan["horizon_years"] < 3:
            amount, reason = 0.0, "计划持有不足三年，与此高波动个股方案不匹配，请调整计划或单独复核。"
        else:
            base_remaining = max(0.0, monthly * weight / 100 - spent[s])
            # Never borrow another symbol's budget automatically. An attractive
            # price can bring forward the remainder of its planned monthly pot.
            fraction = (plan.get("attractive_installment_pct", 100) if item["decision"] == "INCREASE" else plan.get("normal_installment_pct", 50)) / 100
            if s in HIGH_UNCERTAINTY:
                fraction = min(fraction, plan.get("higher_risk_installment_pct", 25) / 100)
            amount = min(base_remaining * fraction, remaining,
                         max(0.0, total * plan["max_single_pct"] / 100 - positions[s]))
            for key in relevant:
                amount = min(amount, max(0.0, total * plan["max_theme_pct"] / 100 - theme_used[key]))
            amount = math.floor(max(0.0, amount) * 100) / 100
            allocations[s] = amount
            remaining -= amount
            for key in relevant:
                theme_used[key] += amount
            reason = f"按该股本月剩余额度的 {fraction * 100:g}% 分批，再受预算和直接持仓上限约束。持仓估算含快照后已确认投入。"
            if amount == 0:
                reason = "本月额度已用完，或已达到你设置的单股/主题上限。"
            if positions[s] == 0 and spent[s] == 0 and amount > 0 and item["decision"] == "NORMAL":
                item["decision"], item["decision_label"] = "START", LABELS["START"]
        item["budget"] = {"amount_usd": amount, "label": "金额待填写计划" if amount is None else f"本期参考上限 ${amount:,.2f}",
                          "reason": reason, "spent_this_month_usd": spent[s], "month": current_period(now),
                          "note": "仅供人工决定，不是委托或成交；核心基金未穿透，主题敞口仅按已填直接持仓计算。"}
    return {"ready": not missing and not allocation_gaps, "plan_fields_ready": not missing,
            "allocation_ready": not missing and not allocation_gaps, "allocation_gaps": allocation_gaps,
            "gaps": missing + (["部分标的持仓或分配资料待补齐"] if allocation_gaps else []), "month": current_period(now),
            "budget_usd": monthly if monthly else None, "confirmed_spent_usd": sum(spent.values()),
            "proposed_usd": round(sum(allocations.values()), 2), "remaining_unallocated_usd": round(remaining, 2),
            "note": "预算和持仓只在本机计算，不发送给模型；过去建议不代表已经买入。"}

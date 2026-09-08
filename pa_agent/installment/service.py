"""Manual orchestration, immutable evidence snapshots and private local plans."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from pa_agent.monitoring.calendar import Calendars
from pa_agent.monitoring.market import PublicClient
from pa_agent.monitoring.service import RefreshBusy, atomic_json, default_data_root
from .ai import AnalysisError, DeepSeekResearch, PROMPT_VERSION, evidence_fingerprint, public_packet
from .decision import DECISION_VERSION, allocate, assess, missing_assessment
from .metrics import price_context, reconcile_valuation
from .models import SYMBOLS, current_period, default_plan, finite, validate_plan
from .sources import PublicResearchClient

RUN_ID = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{12}")
SCHEMA = 1
MAX_SNAPSHOT = 12_582_912


def research_root():
    explicit = os.environ.get("VERDICTQUANT_RESEARCH_ROOT")
    return Path(explicit).expanduser().resolve() if explicit else default_data_root().parent / "installment"


def _read_json(path: Path, limit=MAX_SNAPSHOT):
    if path.stat().st_size > limit:
        raise ValueError("LOCAL_FILE_TOO_LARGE")
    return json.loads(path.read_text(encoding="utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError("NON_FINITE_JSON")))


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()


class InstallmentService:
    def __init__(self, data_root: Path | None = None, market_client=None, research_client=None,
                 analyst=None, symbols=None, clock=None, document_collector=None):
        self.root = (data_root or research_root()).resolve()
        self.symbols = tuple(symbols or SYMBOLS)
        if set(self.symbols) - set(SYMBOLS) or len(set(self.symbols)) != len(self.symbols):
            raise ValueError("RESEARCH_UNIVERSE_INVALID")
        self.clock = clock or (lambda: datetime.now(UTC))
        self.calendars = Calendars()
        policy = json.loads((Path(__file__).parents[1] / "monitoring/policy.json").read_text(encoding="utf-8"))
        self.market = market_client or PublicClient(policy, self.calendars)
        self.public = research_client or PublicResearchClient(cache_dir=self.root / "public-cache")
        self.analyst = analyst
        self.document_collector = document_collector

    def load_engine(self):
        path = self.root / "engine.json"
        value = _read_json(path, 10000) if path.is_file() else {"kind": "codex_cli", "model": "gpt-5.3-codex-spark", "reasoning_effort": "high"}
        if value.get("kind") == "api":
            return {"kind": "api"}
        if value.get("kind") != "codex_cli" or not re.fullmatch(r"gpt-[a-zA-Z0-9._-]{1,70}", str(value.get("model"))) or value.get("reasoning_effort") not in {"low", "medium", "high", "xhigh"}:
            raise ValueError("研究引擎配置无效。")
        return {k: value[k] for k in ("kind", "model", "reasoning_effort")}

    def save_engine(self, value):
        if not isinstance(value, dict):
            raise ValueError("研究引擎配置无效。")
        if value.get("kind") == "api":
            safe = {"kind": "api"}
        elif value.get("kind") == "codex_cli" and re.fullmatch(r"gpt-[a-zA-Z0-9._-]{1,70}", str(value.get("model"))) and value.get("reasoning_effort") in {"low", "medium", "high", "xhigh"}:
            safe = {k: value[k] for k in ("kind", "model", "reasoning_effort")}
        else:
            raise ValueError("研究引擎配置无效。")
        self.root.mkdir(parents=True, exist_ok=True)
        atomic_json(self.root / "engine.json", safe)
        return safe

    def load_plan(self):
        path = self.root / "plan.json"
        return validate_plan(_read_json(path, 100000), self.clock()) if path.is_file() else default_plan(self.clock())

    def save_plan(self, plan):
        value = validate_plan(plan, self.clock())
        value["confirmed_at"] = self.clock().isoformat()
        self.root.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.root / "plan-lock.sqlite3", timeout=2)) as lock, lock:
            lock.execute("BEGIN IMMEDIATE")
            prior = self.load_plan()
            for symbol, position in value["positions_usd"].items():
                if position is None:
                    value["position_confirmed_at"][symbol] = None
                elif plan.get("confirm_holdings") is True or position != prior["positions_usd"].get(symbol):
                    value["position_confirmed_at"][symbol] = value["confirmed_at"]
                else:
                    # Editing a budget or horizon is not a holdings refresh.
                    value["position_confirmed_at"][symbol] = (prior.get("position_confirmed_at", {}).get(symbol)
                                                              or prior.get("confirmed_at"))
            # Preserve each explicit user plan as a local revision. This is not
            # a trade confirmation and is never sent to the LLM or git.
            revision = self.root / "plan-history" / (self.clock().strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:8] + ".json")
            revision.parent.mkdir(exist_ok=True)
            atomic_json(revision, value)
            atomic_json(self.root / "plan.json", value)
        return value

    def contributions(self):
        path = self.root / "contributions.json"
        if not path.is_file():
            return []
        payload = _read_json(path, 2_000_000)
        if payload.get("schema_version") != 1 or not isinstance(payload.get("events"), list):
            raise ValueError("CONTRIBUTION_RECORD_INVALID")
        seen = set()
        for row in payload["events"]:
            if row.get("event_id") in seen or not isinstance(row.get("event_id"), str) or row.get("symbol") not in SYMBOLS:
                raise ValueError("CONTRIBUTION_RECORD_INVALID")
            seen.add(row["event_id"])
            finite(row.get("amount_usd"), minimum=.01, maximum=1_000_000_000)
            if not re.fullmatch(r"20\d\d-(0[1-9]|1[0-2])", str(row.get("period"))):
                raise ValueError("CONTRIBUTION_RECORD_INVALID")
        return payload["events"]

    def record_contribution(self, symbol, amount_usd, period, source_ref="用户在软件确认", event_id=None):
        if symbol not in SYMBOLS or period != current_period(self.clock()):
            raise ValueError("仅可记录当前月份、研究名单内股票的已投入金额。")
        amount = finite(amount_usd, minimum=.01, maximum=1_000_000_000)
        if not isinstance(source_ref, str) or not source_ref.strip() or len(source_ref) > 500:
            raise ValueError("必须保留已投入确认的来源。")
        identifier = event_id or uuid4().hex
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{8,80}", identifier):
            raise ValueError("确认记录编号无效。")
        self.root.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.root / "plan-lock.sqlite3", timeout=2)) as lock, lock:
            lock.execute("BEGIN IMMEDIATE")
            rows = self.contributions()
            prior = next((x for x in rows if x["event_id"] == identifier), None)
            if prior:
                if (prior["symbol"], prior["amount_usd"], prior["period"]) != (symbol, amount, period):
                    raise ValueError("同一确认记录不能对应不同投入。")
                return
            rows.append({"event_id": identifier, "symbol": symbol, "amount_usd": amount,
                         "period": period, "source_ref": source_ref, "confirmed_at": self.clock().isoformat(),
                         "origin": "explicit_user_confirmation", "broker_verified": False})
            atomic_json(self.root / "contributions.json", {"schema_version": 1, "events": rows})

    def _read_run(self, run_id):
        if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
            raise ValueError("RESEARCH_RUN_ID_INVALID")
        path = (self.root / "runs" / run_id / "bundle.json").resolve()
        if not path.is_relative_to(self.root / "runs"):
            raise ValueError("RESEARCH_PATH_INVALID")
        value = _read_json(path)
        if value.get("schema_version") != SCHEMA or value.get("run_id") != run_id or value.get("orders") is not False or value.get("manual_only") is not True:
            raise ValueError("RESEARCH_SNAPSHOT_INVALID")
        return value, path

    def load_run(self, run_id):
        result, _ = self._read_run(run_id)
        result["historical"] = True
        result["actionable"] = False
        return result

    def history(self):
        directory = self.root / "runs"
        if not directory.is_dir():
            return []
        result = []
        for path in sorted(directory.iterdir(), reverse=True):
            if not RUN_ID.fullmatch(path.name):
                continue
            try:
                row, _ = self._read_run(path.name)
                result.append({k: row.get(k) for k in ("run_id", "generated_at", "summary", "provider", "price_as_of")})
            except (ValueError, KeyError, TypeError, OSError):
                continue
            if len(result) >= 40:
                break
        return result

    def latest(self):
        pointer = self.root / "current.json"
        if not pointer.is_file():
            return None
        ref = _read_json(pointer, 4096)
        result, path = self._read_run(ref["run_id"])
        if hashlib.sha256(path.read_bytes()).hexdigest() != ref["sha256"]:
            raise ValueError("RESEARCH_SNAPSHOT_HASH_MISMATCH")
        now = self.clock()
        reasons = []
        if result.get("decision_policy_version") != DECISION_VERSION:
            reasons.append("分析规则已更新，旧判断需重新校验")
        if result.get("engine") != self.load_engine():
            reasons.append("研究引擎已切换，当前显示的是旧引擎结果")
        try:
            if now > datetime.fromisoformat(result["valid_until"]):
                reasons.append("公开消息与模型判断已超过复核时限")
            expected = self.calendars.latest_completed(now, "US").isoformat()
            if any(x.get("price", {}).get("date") != expected for x in result["assessments"] if x.get("price", {}).get("close") is not None):
                reasons.append("已有更新的完成交易日，需要刷新行情与估值")
        except (ValueError, TypeError, KeyError):
            reasons.append("有效期或交易日无法校验")
        result["expired"] = bool(reasons)
        result["expiry_reasons"] = reasons
        result["actionable"] = not reasons
        try:
            plan = self.load_plan()
            # Only budget is recomputed locally; stored company judgment and
            # its source/prompt versions are never rewritten on open.
            result["plan_status"] = allocate(result["assessments"], plan, self.contributions(), now)
            result["plan_changed"] = _hash(plan) != result.get("plan_hash")
        except (OSError, ValueError, TypeError):
            result["plan_status"] = {"ready": False, "gaps": ["本地投资计划或已投入记录需检查"]}
            reasons.append("本地投资计划或已投入记录无法校验")
        if reasons:
            result["actionable"] = False
            for item in result["assessments"]:
                item["budget"] = {"amount_usd": None, "label": "旧分析仅供回看", "reason": "；".join(reasons)}
        return result

    def _collect_one(self, symbol, now, cancelled):
        if cancelled and cancelled.is_set():
            raise CancelledError()
        research = self.public.fetch(symbol, now, cancelled.is_set if cancelled else None)
        try:
            asset = self.market.equity(symbol, "US", now)
            price = price_context(asset, symbol)
        except CancelledError:
            raise
        except Exception:
            price = price_context(None, symbol)
        collector = self.document_collector
        if collector is None:
            try:
                from .documents import collect_documents
                collector = collect_documents
            except ImportError:
                collector = None
        if collector:
            docs = collector(research.get("filings", []), now, cache_dir=self.root / "document-cache",
                             cancelled=cancelled.is_set if cancelled else None)
            research["documents"] = docs.get("documents", [])
            research["sources"].extend(docs.get("sources", []))
            research["errors"].extend(docs.get("errors", []))
        if self.document_collector is None and symbol in {"NVDA", "COIN"}:
            from .issuer_news import collect_issuer_news
            official = collect_issuer_news(symbol, now, cache_dir=self.root / "issuer-cache", cancelled=cancelled.is_set if cancelled else None)
            research["documents"] = [*official.get("documents", []), *research.get("documents", [])][:2]
            research["sources"].extend(official.get("sources", []))
            research["errors"].extend(official.get("errors", []))
        return price, reconcile_valuation(research)

    def refresh(self, cancelled=None, progress=None):
        self.root.mkdir(parents=True, exist_ok=True)
        lock = sqlite3.connect(self.root / "refresh-lock.sqlite3", timeout=.1)
        try:
            try:
                lock.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError:
                raise RefreshBusy("另一个窗口正在更新分批投资研究。") from None
            if cancelled and cancelled.is_set():
                raise CancelledError()
            now = self.clock()
            if hasattr(self.public, "begin_refresh"):
                self.public.begin_refresh()
            prices, research, errors = {}, {}, []
            if progress:
                progress("正在核对股票身份、公开财报、新闻和完成交易日行情…")
            with ThreadPoolExecutor(max_workers=3, thread_name_prefix="public-research") as pool:
                futures = {pool.submit(self._collect_one, s, now, cancelled): s for s in self.symbols}
                for future in as_completed(futures):
                    s = futures[future]
                    if cancelled and cancelled.is_set():
                        for pending in futures:
                            pending.cancel()
                        raise CancelledError()
                    try:
                        prices[s], research[s] = future.result()
                    except CancelledError:
                        raise
                    except Exception as exc:
                        prices[s] = price_context(None, s)
                        research[s] = {"sources": [], "errors": ["PUBLIC_RESEARCH_FAILED:" + type(exc).__name__]}
                        errors.append(s + "：部分公开资料获取失败")
                    if progress:
                        progress(f"公开资料完成 {len(prices)}/{len(self.symbols)}；当前 {s}")
            if not any(x.get("qualified") for x in prices.values()):
                raise AnalysisError("没有取得有效的当前股票行情，保留上次结果。")
            packets = [public_packet(s, prices[s], research[s]) for s in self.symbols]
            # Only verified identities are sent for research. Failed peers stay
            # visible as missing, instead of invalidating every other symbol.
            qualified_packets = [p for p in packets if (p.get("issuer") or {}).get("status") == "verified" and p["price"].get("qualified")]
            if not qualified_packets:
                errors.append("本次没有同时通过发行人身份与行情校验的标的，未调用模型；请查看各股数据来源错误。")
            model_result, reused = {"judgments": {}}, False
            model_times, reused_symbols = {}, []
            model_name = None
            engine = self.load_engine()
            try:
                if self.analyst is not None:
                    analyst = self.analyst
                elif engine["kind"] == "codex_cli":
                    from .codex_provider import CodexResearch
                    analyst = CodexResearch(engine["model"], engine["reasoning_effort"])
                else:
                    analyst = DeepSeekResearch()
                # Injectable test analysts need no real credentials.
                if isinstance(analyst, DeepSeekResearch):
                    model_name = analyst.provider_loader().model
                else:
                    model_name = getattr(analyst, "model", "isolated-test")
                model_tag = json.dumps(engine, sort_keys=True) + model_name
                prior_snapshot = None
                try:
                    prior_snapshot = self.latest()
                except (ValueError, KeyError, TypeError, OSError):
                    pass
                prior_rows = {x["symbol"]: x for x in (prior_snapshot or {}).get("assessments", [])}
                pending, cache_paths = [], {}
                for packet in qualified_packets:
                    symbol = packet["symbol"]
                    digest = evidence_fingerprint([packet], model_tag)
                    cache_path = self.root / "judgment-cache" / (digest + ".json")
                    cache_paths[symbol] = (cache_path, digest)
                    candidate, made_at = None, None
                    if cache_path.is_file():
                        try:
                            cached = _read_json(cache_path)
                            if cached.get("fingerprint") == digest:
                                candidate, made_at = cached["judgment"], cached["created_at"]
                        except (OSError, ValueError, KeyError, TypeError):
                            pass
                    # Reuse a verified prior snapshot during a cache format
                    # migration; never silently replay a different engine.
                    old = prior_rows.get(symbol)
                    if candidate is None and old and prior_snapshot.get("engine") == engine and prior_snapshot.get("prompt_version") == PROMPT_VERSION:
                        old_packet = public_packet(symbol, old["price"], prior_snapshot.get("public_evidence", {}).get(symbol, {}))
                        if evidence_fingerprint([old_packet], model_tag) == digest:
                            candidate = old.get("model_judgment")
                            made_at = old.get("model_generated_at") or prior_snapshot["generated_at"]
                    if candidate is not None and made_at:
                        try:
                            age = (now - datetime.fromisoformat(made_at)).total_seconds()
                            days = candidate.get("review_in_days", 1)
                            if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 30:
                                raise ValueError()
                            checked = assess(symbol, prices[symbol], research[symbol], candidate, now)
                            valid = bool(checked.get("model_judgment"))
                            limit = min(24 * 3600, days * 86400)
                        except (ValueError, KeyError, TypeError):
                            age, limit, valid = -1, 0, False
                        if valid and 0 <= age < limit:
                            model_result["judgments"][symbol] = candidate
                            model_times[symbol] = made_at
                            reused_symbols.append(symbol)
                            continue
                    pending.append(packet)
                if pending:
                    if progress:
                        progress(f"本次只分析 {len(pending)} 只证据已变动或到期的股票；其余复用有效结论。")
                    fresh = analyst.analyze(pending, cancelled, progress)
                    model_result["provider"] = fresh["provider"]
                    for symbol, judgment in fresh["judgments"].items():
                        model_result["judgments"][symbol] = judgment
                        model_times[symbol] = now.isoformat()
                        try:
                            checked = assess(symbol, prices[symbol], research[symbol], judgment, now)
                            valid = bool(checked.get("model_judgment"))
                        except (ValueError, KeyError, TypeError):
                            valid = False
                        if valid:
                            cache_path, digest = cache_paths[symbol]
                            cache_path.parent.mkdir(exist_ok=True)
                            atomic_json(cache_path, {"fingerprint": digest, "created_at": now.isoformat(), "judgment": judgment})
                elif reused_symbols:
                    reused = True
                    model_result["provider"] = {"model": model_name, "kind": engine["kind"], "status": "completed", "usage": {},
                                                "cache_only": True, "public_data_only": True}
            except CancelledError:
                raise
            except AnalysisError as exc:
                errors.append(str(exc))
            except Exception:
                errors.append("模型分析或缓存未通过校验，本次没有采用模型结论。")
            assessments = []
            for s in self.symbols:
                judgment = (model_result or {}).get("judgments", {}).get(s)
                if judgment is None:
                    detail = "发行人身份核验失败，不能把股票代码相同当作同一家公司。" if (research[s].get("identity") or {}).get("status") != "verified" else "本次模型或公开证据未能形成完整判断。"
                    item = missing_assessment(s, prices[s], research[s], detail, now)
                    item["source_errors"] = research[s].get("errors", [])
                else:
                    try:
                        item = assess(s, prices[s], research[s], judgment, now)
                    except (ValueError, KeyError, TypeError) as exc:
                        item = missing_assessment(s, prices[s], research[s], "模型引用、数字口径或输出结构未通过校验。", now)
                        if re.fullmatch(r"MODEL_[A-Z_]+", str(exc)):
                            item["errors"].append(str(exc))
                if s in model_times:
                    item["model_generated_at"] = model_times[s]
                    item["model_reused"] = s in reused_symbols
                    if judgment:
                        original_deadline = datetime.fromisoformat(model_times[s]) + timedelta(days=judgment.get("review_in_days", 1))
                        item["review_at"] = min(datetime.fromisoformat(item["review_at"]), original_deadline).isoformat()
                        item["next_review"] = datetime.fromisoformat(item["review_at"]).date().isoformat()
                assessments.append(item)
            plan = self.load_plan()
            budget = allocate(assessments, plan, self.contributions(), now)
            previous = None
            try:
                previous = self.latest()
            except (ValueError, KeyError, TypeError, OSError):
                pass
            by_symbol = {x["symbol"]: x for x in (previous or {}).get("assessments", [])}
            changes = []
            for item in assessments:
                prior = by_symbol.get(item["symbol"])
                if prior and prior["decision"] != item["decision"]:
                    changes.append({"symbol": item["symbol"], "previous": prior["decision_label"], "current": item["decision_label"],
                                    "reason": item["reasons"][0], "previous_run_id": previous["run_id"]})
            if cancelled and cancelled.is_set():
                raise CancelledError()
            run_id = now.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:12]
            actionable_count = sum(x["decision"] in {"START", "NORMAL", "INCREASE"} for x in assessments)
            missing_count = sum(x["decision"] == "REVIEW" for x in assessments)
            summary = f"{actionable_count} 只具备分批研究条件，{missing_count} 只本次证据不足；投入金额另按本机计划计算。"
            result = {"schema_version": SCHEMA, "run_id": run_id, "generated_at": now.isoformat(),
                      "valid_until": min([now + timedelta(hours=48), *[datetime.fromisoformat(x.get("review_at") or x["next_review"] + "T00:00:00+00:00") for x in assessments]]).isoformat(), "price_as_of": self.calendars.latest_completed(now, "US").isoformat(),
                      "manual_only": True, "orders": False, "account_connections": False, "notifications": False,
                      "assessments": assessments, "public_evidence": research, "summary": summary,
                      "provider": (model_result or {}).get("provider", {"model": model_name, "status": "unavailable", "usage": {}}),
                      "engine": engine,
                      "decision_policy_version": DECISION_VERSION,
                      "model_reused": reused, "prompt_version": PROMPT_VERSION, "plan_status": budget, "plan_hash": _hash(plan),
                      "reused_symbols": reused_symbols,
                      "comparison": {"previous_run_id": (previous or {}).get("run_id"), "changes": changes},
                      "errors": errors, "expired": False, "actionable": True,
                      "limitations": ["这是公开资料与AI情景判断，不是低点预测或收益保证。", "新闻覆盖有限；没有新闻不表示没有风险。",
                                      "估值区间是模型假设，必须连同依据和反例阅读。", "核心基金维持观察，不生成自动赎回或卖出指令。"]}
            directory = self.root / "runs" / run_id
            directory.mkdir(parents=True)
            raw = atomic_json(directory / "bundle.json", result)
            atomic_json(self.root / "current.json", {"run_id": run_id, "sha256": hashlib.sha256(raw).hexdigest()})
            lock.commit()
            if progress:
                progress("分析完成；来源、假设、缺口和历史记录已保存。")
            return result
        finally:
            lock.close()

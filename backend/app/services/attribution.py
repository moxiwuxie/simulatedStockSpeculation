"""模式归因分析 —— 基于训练记录清洗筛选, 输出一套完整的交易模式

输入: 训练会话 (training_sessions) + 逐根决策 (training_bars)
输出: 概览 / 因子分析 / 操作纪律 / 操作手法 / 仓位控制 / 结论建议

口径说明:
- 仅统计终局且已产生真实盈亏的对局 (settled / blown_up), 进行中/已放弃不计入
- 一笔「交易」= 从空仓到再次空仓的完整回合 (开仓→平仓), 支持多空
- 因子取自开仓时点的价格相对均线 / 前情趋势 / 近期高点等
"""
from __future__ import annotations
import json
import sqlite3
from pathlib import Path
from typing import Optional, List, Dict


def _db_path() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "training.db"


def _ma(closes: List[float], idx: int, n: int) -> Optional[float]:
    """idx 位置(不含)前 n 根收盘均值; 样本不足返回 None"""
    if idx < n or n <= 0:
        return None
    seg = closes[idx - n:idx]
    return sum(seg) / len(seg) if seg else None


def _load_sessions(user_id: Optional[str], strategy_id: Optional[int],
                   category: Optional[str], limit: int) -> List[dict]:
    where = ["s.status IN ('settled','blown_up')"]
    params: list = []
    if user_id and user_id != "default":
        where.append("(s.user_id=? OR s.user_id='default')")
        params.append(user_id)
    if strategy_id is not None:
        if strategy_id == -1:                 # 未归类: strategy_id IS NULL
            where.append("s.strategy_id IS NULL")
        else:
            where.append("s.strategy_id=?")
            params.append(strategy_id)
    if category:
        where.append("s.category=?")
        params.append(category)
    params.append(limit)
    with sqlite3.connect(str(_db_path())) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""SELECT s.*, st.name AS strategy_name
                FROM training_sessions s
                LEFT JOIN strategies st ON st.id = s.strategy_id
                WHERE {' AND '.join(where)}
                ORDER BY s.created_at DESC LIMIT ?""", params).fetchall()
    return [dict(r) for r in rows]


def _load_bars(session_id: str) -> List[dict]:
    with sqlite3.connect(str(_db_path())) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM training_bars WHERE session_id=? ORDER BY bar_index",
            (session_id,)).fetchall()
    return [dict(r) for r in rows]


def _pre_bars(sess: dict) -> List[dict]:
    raw = sess.get("settlement_json")
    if not raw:
        return []
    try:
        return json.loads(raw).get("pre_bars") or []
    except (ValueError, TypeError):
        return []


def _finalize_trip(cur: dict, close_bar: dict, full: List[float], pre_n: int,
                   highs: List[float], bars_total: int) -> dict:
    """补齐一笔完整交易的因子与结果"""
    direction = cur["direction"]
    open_price = cur["open_price"]
    close_price = close_bar["close"]
    qty = cur["open_qty"]
    pnl = (close_price - open_price) * qty * direction
    oi = cur["open_index"]
    gi = pre_n + oi                       # 全序列中的开仓索引
    ma5 = _ma(full, gi, 5)
    ma20 = _ma(full, gi, 20)
    # 开仓前情趋势: 相对 5 根前收盘
    trend = 0
    if gi >= 5 and gi <= len(full):
        base = full[gi - 5]
        if base:
            chg = open_price / base - 1
            trend = 1 if chg > 0.005 else (-1 if chg < -0.005 else 0)
    # 追高: 多头开仓价接近/高于开仓前 5 根最高价
    # 注: bars 落库可能缺行 (maxidx > 行数), 切片可能为空, 需容错
    recent_win = highs[max(0, oi - 5):oi] if oi > 0 else []
    recent_high = max(recent_win) if recent_win else None
    chase = 1 if (direction > 0 and recent_high and open_price >= recent_high * 0.99) else 0
    equity = cur.get("open_equity") or 0
    pos_ratio = (qty * open_price / equity) if equity else 0
    ci = close_bar["bar_index"]
    return {
        "direction": "long" if direction > 0 else "short",
        "open_index": oi, "close_index": ci,
        "open_price": round(open_price, 2), "close_price": round(close_price, 2),
        "qty": qty, "pnl": round(pnl, 2), "win": pnl > 0,
        "hold_bars": max(0, ci - oi),
        "ma5": round(ma5, 2) if ma5 else None,
        "ma20": round(ma20, 2) if ma20 else None,
        "above_ma5": (open_price >= ma5) if ma5 else None,
        "above_ma20": (open_price >= ma20) if ma20 else None,
        "ma20_gap": round((open_price - ma20) / ma20 * 100, 2) if ma20 else None,
        "trend": trend, "chase": chase,
        "pos_ratio": round(pos_ratio, 4),
        # 卖出时机: 是否拖到本局最后 (近似"被动平仓")
        "held_to_end": ci >= bars_total - 1,
    }


def _extract_trips(bars: List[dict], pre_bars: List[dict], bars_total: int) -> List[dict]:
    """从逐根决策中还原完整交易回合 (空仓→持仓→空仓)"""
    if not bars:
        return []
    closes = [b["close"] for b in bars]
    highs = [b.get("high") or b["close"] for b in bars]
    pre_closes = [p["close"] for p in pre_bars]
    full = pre_closes + closes
    pre_n = len(pre_closes)
    trips: List[dict] = []
    prev_pos = 0
    cur: Optional[dict] = None
    for b in bars:
        pos_after = b.get("pos_after") or 0
        if prev_pos == 0 and pos_after != 0:
            cur = {
                "open_index": b["bar_index"], "open_date": b["date"],
                "open_price": b["close"], "direction": 1 if pos_after > 0 else -1,
                "open_qty": abs(pos_after), "max_qty": abs(pos_after),
                "open_equity": b.get("equity") or 0,
            }
        elif prev_pos != 0 and pos_after == 0 and cur:
            trips.append(_finalize_trip(cur, b, full, pre_n, highs, bars_total))
            cur = None
        elif cur and pos_after != 0:
            # 加仓/减仓: 记录最大持仓 (用于仓位估算), 方向未反转
            cur["max_qty"] = max(cur.get("max_qty", 0), abs(pos_after))
            if abs(pos_after) > abs(prev_pos) and (pos_after > 0) == (cur["direction"] > 0):
                cur["open_qty"] = abs(pos_after)
        prev_pos = pos_after
    return trips


def _stat(trips: List[dict]) -> dict:
    n = len(trips)
    if n == 0:
        return {"trades": 0, "win_rate": 0, "avg_pnl": 0, "total_pnl": 0, "avg_hold": 0}
    wins = [t["pnl"] for t in trips if t["pnl"] > 0]
    return {
        "trades": n,
        "win_rate": round(len(wins) / n * 100, 1),
        "avg_pnl": round(sum(t["pnl"] for t in trips) / n, 2),
        "total_pnl": round(sum(t["pnl"] for t in trips), 2),
        "avg_hold": round(sum(t["hold_bars"] for t in trips) / n, 1),
    }


def _factor(name: str, rule, trips: List[dict]) -> dict:
    """按 rule(trip) 分组统计"""
    groups: Dict[str, List[dict]] = {}
    for t in trips:
        label = rule(t)
        if label is None:
            continue
        groups.setdefault(label, []).append(t)
    buckets = [{"label": k, **_stat(v)} for k, v in groups.items()]
    buckets.sort(key=lambda x: x["trades"], reverse=True)
    return {"name": name, "buckets": buckets}


def analyze(user_id: Optional[str] = None, strategy_id: Optional[int] = None,
            category: Optional[str] = None, limit: int = 200) -> dict:
    sessions = _load_sessions(user_id, strategy_id, category, limit)
    all_trips: List[dict] = []
    per_session: List[dict] = []
    for s in sessions:
        bars = _load_bars(s["session_id"])
        if not bars:
            continue
        trips = _extract_trips(bars, _pre_bars(s), s.get("bars_used") or len(bars))
        all_trips.extend(trips)
        per_session.append({
            "session_id": s["session_id"], "code": s.get("code"),
            "code_name": s.get("code_name"), "pnl": s.get("pnl"),
            "pnl_pct": s.get("pnl_pct"), "status": s.get("status"),
            "blown_up": s.get("blown_up"), "max_drawdown": s.get("max_drawdown"),
            "trips": len(trips),
        })

    n_sess = len(sessions)
    overview = _overview(sessions, all_trips)

    # ---------- 因子分析 ----------
    if all_trips:
        factors = [
            _factor("买入位置 (相对MA20)", lambda t: ("MA20上方" if t["above_ma20"]
                   else "MA20下方") if t["above_ma20"] is not None else None, all_trips),
            _factor("买入位置 (相对MA5)", lambda t: ("MA5上方" if t["above_ma5"]
                   else "MA5下方") if t["above_ma5"] is not None else None, all_trips),
            _factor("开仓前情趋势", lambda t: {1: "上升趋势", -1: "下降趋势", 0: "震荡整理"}[t["trend"]], all_trips),
            _factor("持仓周期", lambda t: ("1-3根" if t["hold_bars"] <= 3
                   else "4-10根" if t["hold_bars"] <= 10 else "11根以上"), all_trips),
            _factor("仓位轻重", lambda t: ("轻仓 <20%" if t["pos_ratio"] < 0.2
                   else "中仓 20-50%" if t["pos_ratio"] < 0.5 else "重仓 >50%"), all_trips),
            _factor("交易方向", lambda t: "多头" if t["direction"] == "long" else "空头", all_trips),
        ]
    else:
        factors = []

    # ---------- 操作纪律 ----------
    discipline = _discipline(all_trips, per_session)

    # ---------- 操作手法 ----------
    tactics = _tactics(all_trips)

    # ---------- 仓位控制 ----------
    positioning = _positioning(all_trips)

    # ---------- 逻辑推演 (观察→依据→推断→行动) ----------
    reasoning = _reasoning(overview, all_trips)

    # ---------- 结论 (建议与推演同源) ----------
    conclusion = _conclusion(overview, discipline, factors, all_trips, reasoning)

    return {
        "scope": {
            "sessions": n_sess,
            "settled": sum(1 for s in sessions if s.get("status") == "settled"),
            "blown_up": sum(1 for s in sessions if s.get("status") == "blown_up"),
            "strategy": ("默认" if strategy_id == -1 else
                         (sessions[0].get("strategy_name") if sessions and strategy_id is not None
                          else "全部战法")),
            "category": category or "全部品类",
            "trips": len(all_trips),
        },
        "overview": overview,
        "factors": factors,
        "discipline": discipline,
        "tactics": tactics,
        "positioning": positioning,
        "reasoning": reasoning,
        "conclusion": conclusion,
    }


def _overview(sessions: List[dict], trips: List[dict]) -> dict:
    n = len(trips)
    if n == 0:
        return {"sessions": len(sessions), "trips": 0, "win_rate": 0, "pnl_ratio": 0,
                "expectancy": 0, "profit_factor": 0, "avg_pnl_pct": 0, "total_pnl": 0,
                "max_drawdown": 0, "avg_hold": 0, "blowup_rate": 0}
    wins = [t["pnl"] for t in trips if t["pnl"] > 0]
    losses = [t["pnl"] for t in trips if t["pnl"] < 0]
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "sessions": len(sessions),
        "trips": n,
        "win_rate": round(len(wins) / n * 100, 1),
        "pnl_ratio": round(avg_win / avg_loss, 2) if avg_loss else (round(avg_win, 2) if avg_win else 0),
        "expectancy": round(sum(t["pnl"] for t in trips) / n, 2),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else (round(gross_win, 2) if gross_win else 0),
        "avg_pnl_pct": round(sum(s.get("pnl_pct") or 0 for s in sessions) / len(sessions), 2) if sessions else 0,
        "total_pnl": round(sum(s.get("pnl") or 0 for s in sessions), 2),
        "max_drawdown": round(max((s.get("max_drawdown") or 0) for s in sessions), 2) if sessions else 0,
        "avg_hold": round(sum(t["hold_bars"] for t in trips) / n, 1),
        "blowup_rate": round(sum(1 for s in sessions if s.get("blown_up")) / len(sessions) * 100, 1) if sessions else 0,
    }


def _hold_split(trips: List[dict]) -> dict:
    """盈亏持有周期 (盈利单/亏损单的平均持仓根数)"""
    wins = [t for t in trips if t["win"]]
    losses = [t for t in trips if not t["win"]]
    return {
        "w_hold": round(sum(t["hold_bars"] for t in wins) / len(wins), 1) if wins else 0,
        "l_hold": round(sum(t["hold_bars"] for t in losses) / len(losses), 1) if losses else 0,
        "w_n": len(wins), "l_n": len(losses),
    }


def _discipline(trips: List[dict], per_session: List[dict]) -> List[dict]:
    out: List[dict] = []
    n = len(trips)
    if n == 0:
        return out
    longs = [t for t in trips if t["direction"] == "long"]
    chase = [t for t in longs if t["chase"]]
    out.append({
        "name": "追高倾向",
        "value": f"{round(len(chase) / len(longs) * 100, 1)}%" if longs else "--",
        "comment": ("买入时多处于近期高点附近, 容易买在短期顶部" if longs and len(chase) / len(longs) > 0.5
                    else "买入位置较克制, 追高现象不明显" if longs else "无多头样本"),
    })
    avg_trips = round(sum(s["trips"] for s in per_session) / len(per_session), 1) if per_session else 0
    out.append({
        "name": "交易频率",
        "value": f"{avg_trips} 笔/局",
        "comment": ("交易较频繁, 注意手续费与情绪化操作" if avg_trips > 8
                    else "交易节奏偏稳健"),
    })
    losers = [t for t in trips if not t["win"]]
    active_stop = [t for t in losers if not t["held_to_end"]]
    out.append({
        "name": "止损纪律",
        "value": f"{round(len(active_stop) / len(losers) * 100, 1)}%" if losers else "--",
        "comment": ("亏损单中多数能主动了结, 止损纪律较好" if losers and len(active_stop) / len(losers) >= 0.5
                    else "亏损单常拖到结算被动平仓, 缺少主动止损" if losers else "无亏损样本"),
    })
    hs = _hold_split(trips)
    out.append({
        "name": "盈亏持有周期",
        "value": f"盈利 {hs['w_hold']} 根 / 亏损 {hs['l_hold']} 根",
        "comment": ("拿亏单时间明显长于盈利单, 存在『截断利润、放任亏损』倾向"
                    if hs["l_hold"] > hs["w_hold"] * 1.3
                    else "盈亏持有周期较为合理"),
    })
    return out


def _tactics(trips: List[dict]) -> dict:
    buy_trend = {"上升趋势": 0, "下降趋势": 0, "震荡整理": 0}
    sell_win = sell_loss = 0
    for t in trips:
        buy_trend[{1: "上升趋势", -1: "下降趋势", 0: "震荡整理"}[t["trend"]]] += 1
        if t["win"]:
            sell_win += 1
        else:
            sell_loss += 1
    total = len(trips) or 1
    return {
        "buy_timing": [{"label": k, "trades": v, "pct": round(v / total * 100, 1)}
                       for k, v in buy_trend.items()],
        "sell_timing": [
            {"label": "盈利了结", "trades": sell_win, "pct": round(sell_win / total * 100, 1)},
            {"label": "亏损离场", "trades": sell_loss, "pct": round(sell_loss / total * 100, 1)},
        ],
    }


def _positioning(trips: List[dict]) -> dict:
    buckets = {"轻仓 <20%": [], "中仓 20-50%": [], "重仓 >50%": []}
    for t in trips:
        if t["pos_ratio"] < 0.2:
            buckets["轻仓 <20%"].append(t)
        elif t["pos_ratio"] < 0.5:
            buckets["中仓 20-50%"].append(t)
        else:
            buckets["重仓 >50%"].append(t)
    rows = [{"label": k, **_stat(v)} for k, v in buckets.items()]
    heavy = buckets["重仓 >50%"]
    heavy_loss = [t for t in heavy if not t["win"]]
    return {
        "buckets": rows,
        "heavy_loss_rate": round(len(heavy_loss) / len(heavy) * 100, 1) if heavy else 0,
    }


def _item(topic: str, observation: str, evidence: str, inference: str, action: str,
          n: int, diff: float, base: float) -> dict:
    """构造一条「观察 → 依据 → 推断 → 行动」推演; 置信度由样本量决定, 严重度由效果差决定"""
    conf = "high" if n >= 10 else ("medium" if n >= 6 else "low")
    sev = "high" if (n >= 5 and diff >= max(1.0, abs(base))) else ("medium" if diff > 0 else "low")
    return {"topic": topic, "severity": sev, "confidence": conf,
            "observation": observation, "evidence_text": evidence,
            "inference": inference, "action": action, "_n": n, "_diff": round(diff, 2)}


def _pos_label(t: dict) -> Optional[str]:
    """开仓时相对 MA20 的位置 (乖离分档)"""
    g = t.get("ma20_gap")
    if g is None:
        return None
    if g > 3:
        return "MA20上方 >3%"
    if g >= -3:
        return "MA20附近 ±3%"
    return "MA20下方 <-3%"


def _span(buckets: List[dict], min_each: int = 3):
    """挑出「平均收益最好/最差」的两组; 不满足样本门槛返回 None"""
    valid = [b for b in buckets if b["trades"] >= min_each]
    if len(valid) < 2:
        return None
    best = max(valid, key=lambda b: b["avg_pnl"])
    worst = min(valid, key=lambda b: b["avg_pnl"])
    if best["label"] == worst["label"]:
        return None
    n = sum(b["trades"] for b in valid)
    return best, worst, n, round(best["avg_pnl"] - worst["avg_pnl"], 2)


def _bucketize(trips: List[dict], keyfn) -> Dict[str, List[dict]]:
    """按 keyfn(trip) 分组 (跳过返回 None 的 trip)"""
    groups: Dict[str, List[dict]] = {}
    for t in trips:
        label = keyfn(t)
        if label is None:
            continue
        groups.setdefault(label, []).append(t)
    return groups


def _grouped(trips: List[dict], keyfn) -> List[dict]:
    """分组统计 (label + trades/win_rate/avg_pnl/... )"""
    return [{"label": k, **_stat(v)} for k, v in _bucketize(trips, keyfn).items()]


def _reasoning(overview: dict, trips: List[dict]) -> List[dict]:
    """逻辑推演: 每条含 观察事实 / 数据依据 / 因果推断 / 行动建议

    只输出「样本量够 + 存在明确优劣差」的主题, 避免无的放矢; 最多保留 5 条。
    """
    if not trips:
        return []
    base = overview.get("expectancy") or 0
    out: List[dict] = []

    # 1) 买入择位 (相对 MA20 的乖离)
    sp = _span(_grouped(trips, _pos_label))
    if sp:
        best, worst, n, diff = sp
        if best["avg_pnl"] > 0 > worst["avg_pnl"]:
            out.append(_item(
                "买入择位",
                f"在「{worst['label']}」处开仓 {worst['trades']} 笔, 胜率 {worst['win_rate']}%、"
                f"平均收益 {worst['avg_pnl']}; 而「{best['label']}」{best['trades']} 笔, "
                f"胜率 {best['win_rate']}%、平均收益 {best['avg_pnl']}",
                f"{n} 笔样本, 两组平均收益相差 {diff} (全局期望 {round(base, 2)})",
                f"亏损主要来自在「{worst['label']}」区域接货, 该位置买在短期成本上沿的概率更高; "
                f"位置更优的「{best['label']}」才是收益来源",
                f"把「{worst['label']}」列为观察项: 该条件下先用半仓试探, 确认走强再补到常规仓位",
                n, diff, base))

    # 2) 趋势跟随
    sp = _span(_grouped(trips, lambda t: {1: "上升趋势", 0: "震荡整理", -1: "下降趋势"}[t["trend"]]))
    if sp:
        best, worst, n, diff = sp
        if worst["avg_pnl"] < 0 <= best["avg_pnl"]:
            out.append(_item(
                "趋势跟随",
                f"「{worst['label']}」开仓 {worst['trades']} 笔平均 {worst['avg_pnl']}"
                f"(胜率 {worst['win_rate']}%), 「{best['label']}」{best['trades']} 笔平均 {best['avg_pnl']}",
                f"{n} 笔样本, 最好/最差组平均收益相差 {diff}",
                f"你的收益更依赖「{best['label']}」的市场环境; 在「{worst['label']}」入场时缺乏同等推动力, "
                f"期望收益为负",
                f"在「{worst['label']}」不新开多单 (已有持仓只做减仓/止损), 把仓位留给「{best['label']}」",
                n, diff, base))

    # 3) 追高行为
    sp = _span(_grouped(trips, lambda t: ("追高开仓" if t["chase"] else "非追高开仓")
                        if t["direction"] == "long" else None))
    if sp:
        best, worst, n, diff = sp
        if worst["label"] == "追高开仓" and worst["avg_pnl"] < best["avg_pnl"]:
            out.append(_item(
                "追高行为",
                f"追高开仓 {worst['trades']} 笔胜率 {worst['win_rate']}%、平均 {worst['avg_pnl']}; "
                f"非追高 {best['trades']} 笔胜率 {best['win_rate']}%、平均 {best['avg_pnl']}",
                f"{n} 笔多头样本, 平均收益相差 {diff}",
                "多单在贴近近期高点时入场, 追的是已经走完的行情, 回撤缓冲不够",
                "开仓价距前 5 根最高价不足 1% 时, 延后 1 根再决策 (或把仓位砍半)",
                n, diff, base))

    # 4) 持有周期 (按最差组是「更短」还是「更长」给方向正确的推演, 避免想当然)
    sp = _span(_grouped(trips, lambda t: ("1-3根" if t["hold_bars"] <= 3
                                          else "4-10根" if t["hold_bars"] <= 10 else "11根以上")))
    if sp:
        best, worst, n, diff = sp
        if worst["avg_pnl"] < 0 <= best["avg_pnl"]:
            order = {"1-3根": 0, "4-10根": 1, "11根以上": 2}
            shorter = order.get(worst["label"], 1) < order.get(best["label"], 1)
            if shorter:
                infer = (f"「{worst['label']}」平均收益为负、「{best['label']}」为正, 说明盈利单被过早了结、"
                         f"亏损单又快速认赔, 快速进出整体在贡献亏损")
                action = f"盈利单不要再按「{worst['label']}」的节奏了结, 留到「{best['label']}」的持有时间 (或用移动止损跟随)"
            else:
                infer = (f"「{worst['label']}」平均收益为负、「{best['label']}」为正, 说明拖长持仓带不来额外收益, "
                         f"反而放大回撤与时间成本")
                action = f"持仓进入「{worst['label']}」仍未走出预期时, 强制复查一次并降低仓位"
            out.append(_item(
                "持有周期",
                f"持有「{worst['label']}」的 {worst['trades']} 笔平均 {worst['avg_pnl']}, "
                f"「{best['label']}」{best['trades']} 笔平均 {best['avg_pnl']}",
                f"{n} 笔样本, 平均收益相差 {diff}; 全局平均持仓 {overview['avg_hold']} 根",
                infer, action,
                n, diff, base))

    # 5) 盈亏不对称 (拿亏单比拿盈单久)
    hs = _hold_split(trips)
    if hs["l_n"] >= 5 and hs["w_hold"] > 0 and hs["l_hold"] > hs["w_hold"] * 1.3:
        diff = round(hs["l_hold"] - hs["w_hold"], 1)
        out.append(_item(
            "盈亏不对称",
            f"盈利单平均持有 {hs['w_hold']} 根 ({hs['w_n']} 笔), 亏损单平均持有 {hs['l_hold']} 根 ({hs['l_n']} 笔)",
            f"亏损单持有时间是盈利单的 {round(hs['l_hold'] / hs['w_hold'], 2)} 倍",
            "盈利时过早兑现、亏损时反复等回本, 形成『截断利润、放任亏损』的结构, 长期会拉低盈亏比",
            "给亏损单设硬止损 (参考 -5%~-8% 或固定根数), 盈利单用移动止损跟随而不是主动了结",
            hs["l_n"] + hs["w_n"], diff, base))

    # 6) 仓位控制
    sp = _span(_grouped(trips, lambda t: ("轻仓 <20%" if t["pos_ratio"] < 0.2
                                          else "中仓 20-50%" if t["pos_ratio"] < 0.5 else "重仓 >50%")))
    if sp:
        best, worst, n, diff = sp
        if worst["label"].startswith("重仓") and worst["avg_pnl"] < best["avg_pnl"]:
            out.append(_item(
                "仓位控制",
                f"「{worst['label']}」{worst['trades']} 笔胜率 {worst['win_rate']}%、平均 {worst['avg_pnl']}; "
                f"「{best['label']}」{best['trades']} 笔平均 {best['avg_pnl']}",
                f"{n} 笔样本, 平均收益相差 {diff}",
                "重仓单的择时质量没有同步提升, 等于把小错误的代价直接放大",
                "单笔仓位上限压到 50%, 只有「MA20附近/上升趋势」这类高胜率条件下才允许超过 50%",
                n, diff, base))

    # 7) 退出方式 (是否被动拖到结算)
    sp = _span(_grouped(trips, lambda t: "拖到结算被动平仓" if t["held_to_end"] else "主动了结"))
    if sp:
        best, worst, n, diff = sp
        if worst["label"] == "拖到结算被动平仓" and worst["avg_pnl"] < best["avg_pnl"]:
            out.append(_item(
                "退出方式",
                f"被动拖到结算 {worst['trades']} 笔平均 {worst['avg_pnl']} (胜率 {worst['win_rate']}%), "
                f"主动了结 {best['trades']} 笔平均 {best['avg_pnl']} (胜率 {best['win_rate']}%)",
                f"{n} 笔样本, 平均收益相差 {diff}",
                "被动平仓意味着决策权交给了行情末尾, 通常是「扛不住/懒得管」的结果, 而非计划内退出",
                "结算前最后 3 根内必须主动做一次「留/走」判断, 不要留到系统强制结算",
                n, diff, base))

    rank = {"high": 0, "medium": 1, "low": 2}
    out.sort(key=lambda x: (rank.get(x["severity"], 3), -x["_diff"]))
    out = out[:5]
    for it in out:                       # 去掉内部排序字段
        it.pop("_n", None)
        it.pop("_diff", None)
    return out


def _conclusion(overview: dict, discipline: List[dict], factors: List[dict],
                trips: List[dict], reasoning: Optional[List[dict]] = None) -> dict:
    strengths: List[str] = []
    weaknesses: List[str] = []
    suggestions: List[str] = []
    if not trips:
        return {"profile": "样本不足", "summary": "暂无足够的终局训练记录, 请先完成若干局训练后再生成模式归因。",
                "strengths": [], "weaknesses": [], "suggestions": ["多完成几局训练 (建议 ≥5 局) 以获得可靠归因"]}

    wr = overview["win_rate"]
    pf = overview["profit_factor"]
    if wr >= 50 and pf >= 1.2:
        profile = "稳健盈利型"
    elif wr >= 45 and pf >= 1.0:
        profile = "均衡偏多型"
    elif pf < 1.0 and wr < 45:
        profile = "激进亏损型"
    else:
        profile = "待优化型"

    if overview["pnl_ratio"] >= 1.5:
        strengths.append(f"盈亏比 {overview['pnl_ratio']} 较优, 盈利单的幅度能覆盖亏损单")
    if wr >= 50:
        strengths.append(f"胜率 {wr}% 过半, 决策方向感较好")
    if overview["blowup_rate"] <= 10:
        strengths.append("爆仓率低, 风险控制基本到位")
    disc_map = {d["name"]: d for d in discipline}
    if disc_map.get("止损纪律", {}).get("value") not in (None, "--"):
        try:
            if float(str(disc_map["止损纪律"]["value"]).rstrip("%")) >= 50:
                strengths.append("亏损单能主动止损, 操作纪律较好")
        except ValueError:
            pass

    if pf < 1.0:
        weaknesses.append(f"盈亏比/盈利因子偏低 (PF={pf}), 整体处于亏损状态")
    if wr < 45:
        weaknesses.append(f"胜率仅 {wr}%, 择时准确度不足")
    if disc_map.get("追高倾向", {}).get("value") not in (None, "--"):
        try:
            if float(str(disc_map["追高倾向"]["value"]).rstrip("%")) > 50:
                weaknesses.append("追高倾向明显, 容易买在阶段高点")
        except ValueError:
            pass
    if overview["max_drawdown"] > 20:
        weaknesses.append(f"最大回撤 {overview['max_drawdown']}%, 波动偏大")
    if overview["avg_hold"] <= 2:
        weaknesses.append(f"平均持仓仅 {overview['avg_hold']} 根, 交易过于频繁")

    # 建议与「逻辑推演」同源: 先取推演给出的可执行行动, 再补因子层面的对比结论
    for r in (reasoning or []):
        suggestions.append(f"【{r['topic']}】{r['action']}")

    # 因子层面的建议 (找最优/最差分组, 作为兜底补充)
    funcs = {"买入位置 (相对MA20)": "买入择位", "开仓前情趋势": "趋势跟随",
             "仓位轻重": "仓位管理", "持仓周期": "持有周期"}
    for f in factors:
        if f["name"] not in funcs or len(f["buckets"]) < 2:
            continue
        best = max(f["buckets"], key=lambda b: b["avg_pnl"])
        worst = min(f["buckets"], key=lambda b: b["avg_pnl"])
        if best["avg_pnl"] > 0 >= worst["avg_pnl"]:
            suggestions.append(f"【{funcs[f['name']]}】在「{best['label']}」时平均收益 {best['avg_pnl']}, "
                               f"而「{worst['label']}」为 {worst['avg_pnl']}, 建议多参与前者、回避后者")

    if disc_map.get("盈亏持有周期") and not any("持有周期" in s for s in suggestions):
        suggestions.append("【纪律】盈利单可适当让利润奔跑, 亏损单应更快了结 (避免拖到结算)")
    # 去重 + 截断 (避免建议堆成一堵墙)
    seen = set()
    suggestions = [s for s in suggestions if not (s in seen or seen.add(s))][:6]
    if not suggestions:
        suggestions.append("继续保持当前节奏, 增加训练样本以进一步验证模式稳定性")

    summary = (f"基于 {overview['sessions']} 局 / {overview['trips']} 笔完整交易归因: 整体呈「{profile}」特征。"
               f"胜率 {wr}%、盈亏比 {overview['pnl_ratio']}、盈利因子 {pf}、"
               f"平均持仓 {overview['avg_hold']} 根、最大回撤 {overview['max_drawdown']}%、"
               f"爆仓率 {overview['blowup_rate']}%。")
    return {"profile": profile, "summary": summary, "strengths": strengths,
            "weaknesses": weaknesses, "suggestions": suggestions}

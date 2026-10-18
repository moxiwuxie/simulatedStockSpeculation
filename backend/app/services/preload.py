"""后端预加载 / 预热服务 (需求101602)

背景: 冷启动时若干重接口响应 > 0.5s (看盘全景 ~10s / 板块K线 ~5s / 板块分时 ~4s),
原因是一次性要向上游 (iFinD / 新浪 / akshare) 拉取合并大量数据。各服务虽已带内存缓存,
但「第一次」请求仍要承担全部耗时。

本服务在后台线程中, 依次预热这些重接口, 把结果写入各服务自身的内存缓存;
待前端首次进入对应模块时即可直接命中缓存, 无需等待冷启动。

- `warm_async(feed, registry)`: 幂等启动一次后台预热 (已在跑则直接返回), 立即返回
- `warm_all(feed, registry)`: 同步执行一遍全部预热 (供测试/脚本调用)
- `status()`: 返回预热进度快照 (供 GET /api/preload 展示)
"""
from __future__ import annotations
import threading
import time
from typing import Callable, List, Tuple

_LOCK = threading.Lock()
_FINISHED = False     # 是否已完成过一轮预热 (避免 /api/preload 每次调用都重复预热)
_REWARM_AFTER = 600   # 距上次预热超过该秒数时, 再次调用 /api/preload 会重新预热 (缓存已过期)
# 需求101604: 训练标的池「按需扩容」的并发闸门 (供 /api/training/prime 在池不足时触发)
_POOL_LOCK = threading.Lock()
_POOL_WARMING = False
# 需求101604: 候选池目标规模 —— 池越大, 越不容易被「同账号最近50局排空」而走现场探测慢路径
POOL_MIN = 40
# 需求101605: 日K预热覆盖「全部候选」的上限 —— 随机抽标会落在池内任意候选上,
# 只预热 top-40 会导致约半数开局仍走 akshare 冷取 (~1.7~2.2s)。这里把覆盖面上调到 200。
POOL_WARM_MAX = 200

_STATE = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "elapsed_ms": None,
    "total": 0,
    "done": [],          # [{"name":..., "ms":...}]
    "failed": [],        # [{"name":..., "error":...}]
}


def _warm_training_pool(max_codes: int = POOL_WARM_MAX, min_pool: int = POOL_MIN) -> int:
    """需求101603: 预热「开始训练」依赖 —— 标的池候选 (+ 必要时补探) 的日K

    ``training_mgr.start()`` 随机抽标时按「统计日最新优先」分层取候选, 再拉该标的
    全量日K (akshare 冷取 0.8~2.0s, 是接口 >0.5s 的主因); 池内候选被同账号最近
    50 局排空时还会现场探测最多 12 只 (串行拉日K+分时), 是更慢的路径。

    这里做两件事: ① 按同一优先级预热池内候选的日K; ② 池还不够大时并发补探若干
    注册表标的 (``_stat_for`` 顺手写入池 + 复用本次日K), 既养大候选池、又填热日K缓存,
    使后续开局尽量命中「池 + 缓存」而不是现场探测。
    """
    import random
    from concurrent.futures import ThreadPoolExecutor

    from . import stock_pool as pool
    from .training_session import CATEGORY_BARS_CAP, _augment_amount, _fetch_stock_kline

    def _fetch(c: str):
        return _augment_amount(_fetch_stock_kline(c), "stock")

    # ① 池内候选 (与 start 的排序口径一致: 统计日 / 最新收盘 降序)
    codes: List[str] = []
    for cat in ("stock", "demon"):                       # 股票双盲 1250 / 妖股 250
        try:
            rows = pool.candidates(CATEGORY_BARS_CAP.get(cat, 250) + 30, only_intraday=True)
        except Exception:
            rows = []
        rows.sort(key=lambda r: (r.get("stat_date") or "", r.get("last_close") or 0), reverse=True)
        for r in rows:
            c = r.get("code")
            if c and c not in codes:
                codes.append(c)

    # ② 池不足 → 从注册表补探, 顺带把池养大 (并发上限受 sqlite 写入与上游带宽约束)
    if len(codes) < min_pool:
        try:
            from ..data.stock_registry import StockRegistry
            reg = StockRegistry()
            reg.ensure_ready()
            ledger = [r["code"] for r in reg.list_all() if r.get("market") in ("sh", "sz")]
        except Exception:
            ledger = []
        random.shuffle(ledger)
        for c in ledger:
            if len(codes) >= min_pool:
                break
            if c not in codes:
                codes.append(c)

    codes = codes[:max_codes]
    if not codes:
        return 0

    def _one(c: str) -> bool:
        try:
            return bool(pool._stat_for(c, _fetch))       # 写池 + 命中本次日K缓存
        except Exception:
            return False

    with ThreadPoolExecutor(max_workers=min(8, max(2, len(codes)))) as ex:
        return sum(1 for r in ex.map(_one, codes) if r)


def warm_pool_async(min_pool: int = POOL_MIN) -> dict:
    """需求101604: 后台「扩容」训练标的池 (供 /api/training/prime 在池不足时触发)

    幂等: 同一时刻只跑一个扩容线程; 扩容完成后池内候选更多, 随机开局更容易命中池内
    候选 (而不是现场探测), 同时顺带填热这些标的的日K缓存。
    """
    global _POOL_WARMING
    with _POOL_LOCK:
        if _POOL_WARMING:
            return {"running": True}
        _POOL_WARMING = True

    def _run():
        global _POOL_WARMING
        try:
            n = _warm_training_pool(max_codes=POOL_WARM_MAX, min_pool=min_pool)
            print(f"[Preload] 训练池扩容完成: {n} 只")
        except Exception as e:
            print(f"[Preload] 训练池扩容失败: {e}")
        finally:
            with _POOL_LOCK:
                _POOL_WARMING = False

    threading.Thread(target=_run, name="preload-pool", daemon=True).start()
    return {"running": True}


def _targets(feed, registry) -> List[Tuple[str, Callable]]:
    """返回 [(名称, 可调用)] —— 每个目标内部自行落各自服务的内存缓存"""
    from .watch import real_panorama
    from .boards import list_boards, board_index, board_intraday
    from .index_feed import list_indices, index_kline, index_intraday
    from .market_spot import market_spot

    t: List[Tuple[str, Callable]] = [
        # 需求101605: 自选/动态分组报价底座 —— 全市场快照冷取 20~35s, 必须提前预热,
        # 否则「自选模块首次进入 / 切换分组」会阻塞等到拉取完成。
        ("market.spot", lambda: market_spot.get_spot()),
        ("watch.panorama", lambda: real_panorama()),          # 慢: ~10s (iFinD + 新浪板块)
        ("indices.list", lambda: list_indices()),
        ("index.kline.000001", lambda: index_kline("000001", days=240)),
        ("index.intraday.000001", lambda: index_intraday("000001")),
        ("boards.industry", lambda: list_boards("industry")),
        ("boards.concept", lambda: list_boards("concept")),
        ("stocks.list", lambda: registry.list_all()),          # 大: ~776KB
        ("training.pool_klines", _warm_training_pool),         # 需求101603: 开局随机标的日K
    ]

    # 预热 1 个热门板块的 K 线 + 分时 (覆盖「首点板块」的冷启动; 过多会挤占上游带宽)
    for kind in ("industry", "concept"):
        try:
            for b in (list_boards(kind) or [])[:1]:
                label = b.get("label")
                if not label:
                    continue
                t.append((f"board.kline.{label}",
                          (lambda lb: (lambda: board_index(feed, kind, lb, days=120)))(label)))
                t.append((f"board.intraday.{label}",
                          (lambda lb: (lambda: board_intraday(feed, kind, lb)))(label)))
        except Exception:
            pass
    return t


def warm_all(feed, registry) -> dict:
    """同步执行一遍全部预热, 返回进度快照"""
    global _FINISHED
    targets = _targets(feed, registry)
    _STATE.update(running=True, started_at=time.time(), finished_at=None,
                  elapsed_ms=None, total=len(targets), done=[], failed=[])
    for name, fn in targets:
        t0 = time.time()
        try:
            fn()
            _STATE["done"].append({"name": name, "ms": int((time.time() - t0) * 1000)})
        except Exception as e:
            _STATE["failed"].append({"name": name, "error": str(e)[:200]})
    _STATE["finished_at"] = time.time()
    _STATE["elapsed_ms"] = int((_STATE["finished_at"] - _STATE["started_at"]) * 1000)
    _STATE["running"] = False
    _FINISHED = True
    return status()


def warm_async(feed, registry, force: bool = False) -> dict:
    """幂等启动后台预热: 进行中/刚预热过则直接返回当前快照; force=True 时强制重新预热

    长时间运行 (超过 _REWARM_AFTER) 后, 各服务缓存可能已过期, 此时再次调用会重新预热。
    """
    with _LOCK:
        if _STATE["running"]:
            return status()
        if _FINISHED and not force:
            fin = _STATE.get("finished_at") or 0
            if time.time() - fin < _REWARM_AFTER:
                return status()
        _STATE["running"] = True

    def _run():
        try:
            warm_all(feed, registry)
        except Exception:
            _STATE["running"] = False

    threading.Thread(target=_run, name="preload-warm", daemon=True).start()
    return status()


def status() -> dict:
    """预热进度快照"""
    n = len(_STATE["done"]) + len(_STATE["failed"])
    return {
        "running": _STATE["running"],
        "progress": f"{n}/{_STATE['total']}" if _STATE["total"] else "0/0",
        "elapsed_ms": _STATE["elapsed_ms"],
        "done": list(_STATE["done"]),
        "failed": list(_STATE["failed"]),
    }

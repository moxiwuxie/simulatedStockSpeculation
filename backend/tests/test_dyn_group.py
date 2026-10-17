"""动态分组 (market_spot + dyn_group 服务 + 接口) 后端单测"""
import pytest


# ---------- 假快照数据源 ----------
class _FakeDF:
    def __init__(self, records):
        self._records = records

    def to_dict(self, *_a, **_k):
        return self._records


def _records():
    return [
        # 炸板: 最高触涨停(1870)但收 1800
        {"代码": "sh600519", "名称": "贵州茅台", "最新价": 1800.0, "昨收": 1700.0, "今开": 1710.0,
         "最高": 1870.0, "最低": 1700.0, "成交量": 1000.0, "成交额": 1_000_000.0, "涨跌幅": 5.88},
        # 创业板涨停 (20%)
        {"代码": "sz300750", "名称": "宁德时代", "最新价": 120.0, "昨收": 100.0, "今开": 101.0,
         "最高": 120.0, "最低": 100.0, "成交量": 5000.0, "成交额": 5_000_000.0, "涨跌幅": 20.0},
        # 主板跌停 (10%)
        {"代码": "sh600000", "名称": "浦发银行", "最新价": 9.0, "昨收": 10.0, "今开": 9.5,
         "最高": 9.5, "最低": 9.0, "成交量": 800.0, "成交额": 80_000.0, "涨跌幅": -10.0},
        # 北交所涨停 (30%)
        {"代码": "bj920000", "名称": "安徽凤凰", "最新价": 13.0, "昨收": 10.0, "今开": 10.1,
         "最高": 13.0, "最低": 10.0, "成交量": 300.0, "成交额": 30_000.0, "涨跌幅": 30.0},
        # ST 跌停 (5%)
        {"代码": "sz000004", "名称": "ST国华", "最新价": 9.5, "昨收": 10.0, "今开": 9.8,
         "最高": 9.8, "最低": 9.5, "成交量": 200.0, "成交额": 20_000.0, "涨跌幅": -5.0},
    ]


@pytest.fixture
def spot():
    from app.services.market_spot import MarketSpot
    ms = MarketSpot(ttl=300.0, fetcher=lambda: _FakeDF(_records()))
    return ms.get_spot()


@pytest.fixture
def dg(tmp_path):
    from app.services.dyn_group import DynGroupService
    DynGroupService._instance = None
    svc = DynGroupService()
    svc._db_path = tmp_path / "dyn_groups.db"
    svc._init_db()
    return svc


class TestMarketSpot:
    def test_limit_pct_by_market(self, spot):
        assert spot["600519"]["limit_pct"] == 0.10
        assert spot["300750"]["limit_pct"] == 0.20
        assert spot["920000"]["limit_pct"] == 0.30
        assert spot["000004"]["limit_pct"] == 0.05

    def test_limit_up_down_flags(self, spot):
        assert spot["300750"]["is_limit_up"] is True
        assert spot["920000"]["is_limit_up"] is True
        assert spot["600000"]["is_limit_down"] is True
        assert spot["000004"]["is_limit_down"] is True
        assert spot["600519"]["is_limit_up"] is False

    def test_zhaban_and_upper_shadow(self, spot):
        m = spot["600519"]
        assert m["is_zhaban"] is True
        assert m["upper_shadow"] == 4.12          # (1870-1800)/1700*100
        assert spot["300750"]["is_zhaban"] is False

    def test_code_normalized(self, spot):
        assert set(spot) == {"600519", "300750", "600000", "920000", "000004"}

    def test_fetch_failure_falls_back_to_cache(self):
        from app.services.market_spot import MarketSpot
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                return _FakeDF(_records())
            raise RuntimeError("network down")

        ms = MarketSpot(ttl=0.0, fetcher=flaky)
        first = ms.get_spot()
        assert first
        assert ms.get_spot() == first            # 断连 → 回退旧缓存


class TestEvaluate:
    def test_bool_metric_and_sort(self, spot):
        from app.services.dyn_group import evaluate
        rules = {"logic": "AND", "conditions": [{"metric": "is_limit_up", "op": "eq", "value": True}]}
        hits = evaluate(rules, spot)
        assert [h["code"] for h in hits] == ["920000", "300750"]   # 涨跌幅倒序 30 > 20

    def test_zhaban_metric(self, spot):
        from app.services.dyn_group import evaluate
        rules = {"logic": "AND", "conditions": [{"metric": "is_zhaban", "op": "eq", "value": True}]}
        assert [h["code"] for h in evaluate(rules, spot)] == ["600519"]

    def test_multi_condition_and(self, spot):
        from app.services.dyn_group import evaluate
        rules = {"logic": "AND", "conditions": [
            {"metric": "change_pct", "op": "gt", "value": 0},
            {"metric": "volume", "op": "gte", "value": 1000},
        ]}
        assert [h["code"] for h in evaluate(rules, spot)] == ["300750", "600519"]

    def test_between_and_price_alias(self, spot):
        from app.services.dyn_group import evaluate
        rules = {"logic": "AND", "conditions": [
            {"metric": "price", "op": "between", "value": [9.0, 15.0]},
        ]}
        assert sorted(h["code"] for h in evaluate(rules, spot)) == ["000004", "600000", "920000"]

        # close 与 price 同源
        r2 = {"logic": "AND", "conditions": [{"metric": "close", "op": "gt", "value": 1000}]}
        assert [h["code"] for h in evaluate(r2, spot)] == ["600519"]

    def test_empty_conditions_returns_nothing(self, spot):
        from app.services.dyn_group import evaluate
        assert evaluate({"logic": "AND", "conditions": []}, spot) == []

    def test_unknown_metric_never_matches(self, spot):
        from app.services.dyn_group import evaluate
        rules = {"logic": "AND", "conditions": [{"metric": "nope", "op": "gt", "value": 0}]}
        assert evaluate(rules, spot) == []


class TestNormalizeRules:
    def test_drops_unknown_metric(self):
        from app.services.dyn_group import normalize_rules
        r = normalize_rules({"conditions": [
            {"metric": "nope", "op": "gt", "value": 1},
            {"metric": "change_pct", "op": "gt", "value": 5},
        ]})
        assert r["logic"] == "AND" and len(r["conditions"]) == 1

    def test_empty_raises(self):
        from app.services.dyn_group import normalize_rules
        with pytest.raises(ValueError, match="至少配置一条"):
            normalize_rules({"conditions": []})


class TestDynGroupCRUD:
    def test_presets_always_listed_builtin(self, dg):
        groups = dg.list_groups("u_a")
        assert [g["id"] for g in groups] == ["preset_limit_up", "preset_zhaban", "preset_limit_down"]
        assert all(g["builtin"] for g in groups)

    def test_add_update_delete(self, dg):
        g = dg.add_group("u_a", "强势股", {"conditions": [{"metric": "change_pct", "op": "gt", "value": 9}]})
        assert g["builtin"] is False
        assert g["rules"]["logic"] == "AND"

        up = dg.update_group("u_a", g["id"], name="超强", rules={"conditions": [
            {"metric": "change_pct", "op": "between", "value": [5, 9]}]})
        assert up["name"] == "超强"
        assert up["rules"]["conditions"][0]["op"] == "between"

        assert dg.delete_group("u_a", g["id"]) is True
        assert dg.get_group("u_a", g["id"]) is None

    def test_presets_readonly(self, dg):
        with pytest.raises(ValueError, match="不可修改"):
            dg.update_group("u_a", "preset_limit_up", name="x")
        with pytest.raises(ValueError, match="不可删除"):
            dg.delete_group("u_a", "preset_limit_up")

    def test_duplicate_and_preset_name_conflict(self, dg):
        dg.add_group("u_a", "白酒", {"conditions": [{"metric": "change_pct", "op": "gt", "value": 1}]})
        with pytest.raises(ValueError, match="已存在"):
            dg.add_group("u_a", "白酒", {"conditions": [{"metric": "change_pct", "op": "gt", "value": 2}]})
        with pytest.raises(ValueError, match="预设冲突"):
            dg.add_group("u_a", "涨停股", {"conditions": [{"metric": "change_pct", "op": "gt", "value": 1}]})

    def test_isolated_between_users(self, dg):
        g = dg.add_group("u_a", "白酒", {"conditions": [{"metric": "change_pct", "op": "gt", "value": 1}]})
        assert dg.get_group("u_b", g["id"]) is None
        assert all(x["builtin"] for x in dg.list_groups("u_b"))

    def test_stocks_evaluates_rules(self, dg, monkeypatch):
        from app.services import dyn_group as dgmod
        monkeypatch.setattr(dgmod.market_spot, "get_spot", lambda force=False: _parse(_records()))
        g = dg.add_group("u_a", "涨停股2", {"conditions": [{"metric": "is_limit_up", "op": "eq", "value": True}]})
        hits = dg.stocks("u_a", g["id"])
        assert [h["code"] for h in hits] == ["920000", "300750"]
        # 预设分组同样可求值
        assert [h["code"] for h in dg.stocks("u_a", "preset_zhaban")] == ["600519"]

    def test_recent_metrics_recorded_on_save(self, dg):
        dg.add_group("u_a", "放量", {"conditions": [
            {"metric": "volume", "op": "gt", "value": 900},
            {"metric": "change_pct", "op": "gt", "value": 3}]})
        assert set(dg.recent_metrics("u_a")) == {"volume", "change_pct"}

    def test_recent_metrics_most_recent_first_and_capped(self, dg):
        dg.add_group("u_a", "g1", {"conditions": [{"metric": "volume", "op": "gt", "value": 1}]})
        dg.add_group("u_a", "g2", {"conditions": [{"metric": "change_pct", "op": "gt", "value": 1}]})
        assert dg.recent_metrics("u_a")[0] == "change_pct"
        # 重复使用只刷新时间, 不产生重复项
        dg.add_group("u_a", "g3", {"conditions": [{"metric": "volume", "op": "gt", "value": 1}]})
        assert dg.recent_metrics("u_a") == ["volume", "change_pct"]
        assert len(dg.recent_metrics("u_a", limit=1)) == 1

    def test_recent_metrics_isolated_between_users(self, dg):
        dg.add_group("u_a", "放量", {"conditions": [{"metric": "volume", "op": "gt", "value": 900}]})
        assert dg.recent_metrics("u_a") == ["volume"]
        assert dg.recent_metrics("u_b") == []

    def test_recent_metrics_ignores_unknown_keys(self, dg):
        dg.record_recent("u_a", ["nope", "volume"])
        assert dg.recent_metrics("u_a") == ["volume"]


def _parse(records):
    from app.services.market_spot import MarketSpot
    return MarketSpot(ttl=300.0, fetcher=lambda: _FakeDF(records)).get_spot()


class TestDynGroupEndpoints:
    @pytest.fixture
    def m(self, tmp_path, monkeypatch):
        from app import main
        from app.services import dyn_group as dgmod
        dgmod.dyn_group_svc._db_path = tmp_path / "dyn_groups.db"
        dgmod.dyn_group_svc._init_db()
        monkeypatch.setattr(dgmod.market_spot, "get_spot", lambda force=False: _parse(_records()))
        return main

    def test_metrics_metadata(self, m):
        keys = [x["key"] for x in m.dyn_group_metrics()["metrics"]]
        for k in ("is_limit_up", "is_limit_down", "is_zhaban", "upper_shadow",
                  "change_pct", "volume", "amount", "price", "open", "close"):
            assert k in keys

    def test_list_add_update_delete(self, m):
        assert len(m.dyn_groups_list(user_id="u_e2e")["groups"]) == 3
        g = m.dyn_groups_add(m.DynGroupRequest(
            name="放量", rules={"conditions": [{"metric": "volume", "op": "gt", "value": 900}]}),
            user_id="u_e2e")
        gid = str(g["id"])
        hits = m.dyn_groups_stocks(gid, limit=200, refresh=0, user_id="u_e2e")
        assert set(h["code"] for h in hits["stocks"]) == {"600519", "300750"}
        m.dyn_groups_delete(gid, user_id="u_e2e")
        assert len(m.dyn_groups_list(user_id="u_e2e")["groups"]) == 3

    def test_preset_stocks_by_id(self, m):
        r = m.dyn_groups_stocks("preset_limit_up", limit=10, refresh=0, user_id="u_e2e")
        assert r["total"] == 2

    def test_preview_counts(self, m):
        r = m.dyn_groups_preview(m.DynPreviewRequest(
            rules={"conditions": [{"metric": "upper_shadow", "op": "gt", "value": 2}]}), user_id="u_e2e")
        assert [h["code"] for h in r["stocks"]] == ["600519"]

    def test_preview_rejects_empty_rules(self, m):
        with pytest.raises(m.HTTPException) as e:
            m.dyn_groups_preview(m.DynPreviewRequest(rules={"conditions": []}), user_id="u_e2e")
        assert e.value.status_code == 400

    def test_metrics_metadata_has_category_and_hot(self, m):
        metas = {x["key"]: x for x in m.dyn_group_metrics()["metrics"]}
        assert metas["is_limit_up"]["cat"] == "涨停异动"
        assert metas["upper_shadow"]["cat"] == "技术面"
        assert metas["price"]["cat"] == "价格成交"
        assert metas["is_limit_up"]["hot"] is True
        assert not metas["volume"].get("hot")

    def test_recent_metrics_endpoint_after_save_and_preview(self, m):
        assert m.dyn_groups_recent_metrics(user_id="u_e2e")["metrics"] == []
        m.dyn_groups_add(m.DynGroupRequest(
            name="放量", rules={"conditions": [{"metric": "volume", "op": "gt", "value": 900}]}),
            user_id="u_e2e")
        assert m.dyn_groups_recent_metrics(user_id="u_e2e")["metrics"] == ["volume"]
        m.dyn_groups_preview(m.DynPreviewRequest(
            rules={"conditions": [{"metric": "change_pct", "op": "gt", "value": 0}]}), user_id="u_e2e")
        assert m.dyn_groups_recent_metrics(user_id="u_e2e")["metrics"][0] == "change_pct"

    def test_missing_group_404(self, m):
        with pytest.raises(m.HTTPException) as e:
            m.dyn_groups_stocks("999999", limit=10, refresh=0, user_id="u_e2e")
        assert e.value.status_code == 404

    def test_preset_delete_400(self, m):
        with pytest.raises(m.HTTPException) as e:
            m.dyn_groups_delete("preset_limit_up", user_id="u_e2e")
        assert e.value.status_code == 400


class TestDynGroupGuestGating:
    def test_require_user_id_gates_dyn_endpoints(self):
        from app import main as m
        with pytest.raises(m.HTTPException) as e:
            m.require_user_id("")           # 游客 → 401, 动态分组与自选分组口径一致
        assert e.value.status_code == 401

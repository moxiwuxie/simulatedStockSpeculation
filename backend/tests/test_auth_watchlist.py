"""认证 + 自选分组后端单测 (auth / watchlist 服务)"""
import pytest


@pytest.fixture
def auth(tmp_path):
    from app.services.auth import AuthService
    AuthService._instance = None
    a = AuthService()
    a._db_path = tmp_path / "auth.db"
    a._init_db()
    return a


class TestAuth:
    def test_register_login_logout(self, auth):
        u = auth.register("investor", "secret123", "13800000001")
        assert u["username"] == "investor"
        assert u["user_id"].startswith("u_")
        assert u["phone"] == "13800000001"
        result = auth.login("investor", "secret123")
        assert "token" in result
        assert result["need_bind_phone"] is False
        assert auth.get_user(result["token"])["username"] == "investor"
        auth.logout(result["token"])
        assert auth.get_user(result["token"]) is None

    def test_duplicate_username_raises(self, auth):
        auth.register("dupuser", "secret123", "13800000002")
        with pytest.raises(ValueError, match="已存在"):
            auth.register("dupuser", "secret123", "13800000003")

    def test_wrong_password_raises(self, auth):
        auth.register("badpw", "secret123", "13800000004")
        with pytest.raises(ValueError, match="用户名或密码错误"):
            auth.login("badpw", "wrongpw")

    def test_short_password_raises(self, auth):
        with pytest.raises(ValueError, match="至少 6 位"):
            auth.register("shortpw", "123", "13800000005")

    def test_password_is_hashed(self, auth, tmp_path):
        from sqlite3 import connect
        auth.register("hasher", "secret123", "13800000006")
        conn = connect(str(tmp_path / "auth.db"))
        row = conn.execute("SELECT password_hash FROM users WHERE username='hasher'").fetchone()
        assert row and row[0] != "secret123"
        conn.close()


class TestAuthPhoneEmail:
    """手机号必填/校验/唯一 + 邮箱选填 + 旧账号补绑"""

    def test_phone_required(self, auth):
        with pytest.raises(ValueError, match="手机号必填"):
            auth.register("nophone", "secret123", "")

    def test_phone_format_invalid(self, auth):
        for bad in ("12345", "23800000001", "1380000000a", "1380000000"):
            with pytest.raises(ValueError, match="手机号格式不正确"):
                auth.register("p" + bad, "secret123", bad)

    def test_phone_globally_unique(self, auth):
        auth.register("phone_a", "secret123", "13900000001")
        with pytest.raises(ValueError, match="该手机号已被注册"):
            auth.register("phone_b", "secret123", "13900000001")

    def test_email_optional_and_validated(self, auth):
        u = auth.register("noemail", "secret123", "13900000002")
        assert u["email"] == ""
        u2 = auth.register("withemail", "secret123", "13900000003", "a@b.com")
        assert u2["email"] == "a@b.com"
        with pytest.raises(ValueError, match="邮箱格式不正确"):
            auth.register("bademail", "secret123", "13900000004", "not-an-email")

    def test_profile_phone_bound(self, auth):
        auth.register("profiled", "secret123", "13900000005")
        p = auth.profile(auth._find_user_name("profiled")["user_id"])
        assert p["phone_bound"] is True
        assert p["phone"] == "13900000005"

    def test_legacy_account_needs_bind_and_bind_phone(self, auth, tmp_path):
        """旧账号(无 phone)登录 → need_bind_phone=True; 补绑后为 False"""
        from sqlite3 import connect
        auth.register("legacy", "secret123", "13900000006")
        conn = connect(str(tmp_path / "auth.db"))
        row = conn.execute("SELECT user_id FROM users WHERE username='legacy'").fetchone()
        conn.execute("UPDATE users SET phone=NULL WHERE username='legacy'")
        conn.commit(); conn.close()
        uid = row[0]

        result = auth.login("legacy", "secret123")
        assert result["need_bind_phone"] is True
        assert auth.get_user(result["token"])["phone_bound"] is False

        p = auth.bind_phone(uid, "13900000007")
        assert p["phone_bound"] is True and p["phone"] == "13900000007"
        assert auth.login("legacy", "secret123")["need_bind_phone"] is False

    def test_bind_phone_rejects_duplicate(self, auth, tmp_path):
        from sqlite3 import connect
        auth.register("first", "secret123", "13900000008")
        auth.register("second", "secret123", "13900000009")
        conn = connect(str(tmp_path / "auth.db"))
        conn.execute("UPDATE users SET phone=NULL WHERE username='second'")
        conn.commit()
        uid = conn.execute("SELECT user_id FROM users WHERE username='second'").fetchone()[0]
        conn.close()
        with pytest.raises(ValueError, match="该手机号已被注册"):
            auth.bind_phone(uid, "13900000008")
        with pytest.raises(ValueError, match="手机号格式不正确"):
            auth.bind_phone(uid, "abc")

    def test_migration_adds_columns_to_legacy_db(self, tmp_path):
        """旧库(无 phone/email 列) 经 _init_db 迁移后可用"""
        from sqlite3 import connect
        db = tmp_path / "legacy.db"
        c = connect(str(db))
        c.execute("""CREATE TABLE users (user_id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL,
                     password_hash TEXT NOT NULL, salt TEXT NOT NULL, created_at TEXT)""")
        c.execute("INSERT INTO users VALUES ('u_old','old','h','s','2024-01-01')")
        c.commit(); c.close()

        from app.services.auth import AuthService
        AuthService._instance = None
        a = AuthService()
        a._db_path = db
        a._init_db()
        conn = connect(str(db))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        conn.close()
        assert {"phone", "email"} <= cols
        assert a.profile("u_old")["phone_bound"] is False


@pytest.fixture
def wl(tmp_path):
    from app.services.watchlist import WatchlistService
    WatchlistService._instance = None
    svc = WatchlistService()
    svc._db_path = tmp_path / "watchlist.db"
    svc._init_db()
    return svc


class TestWatchlist:
    def test_default_group_created(self, wl):
        groups = wl.list_groups("u_alice")
        assert len(groups) == 1
        assert groups[0]["name"] == "自选"
        assert groups[0]["items"] == []

    def test_add_item_to_default_group(self, wl):
        wl.add_item("u_alice", "600519", "贵州茅台")
        groups = wl.list_groups("u_alice")
        assert [i["code"] for i in groups[0]["items"]] == ["600519"]

    def test_groups_isolated_between_users(self, wl):
        wl.add_item("u_alice", "600519", "贵州茅台")
        wl.add_item("u_bob", "000001", "平安银行")
        g_alice = wl.list_groups("u_alice")[0]["items"]
        g_bob = wl.list_groups("u_bob")[0]["items"]
        assert [i["code"] for i in g_alice] == ["600519"]
        assert [i["code"] for i in g_bob] == ["000001"]

    def test_custom_group_crud(self, wl):
        g = wl.add_group("u_alice", "白酒")
        assert g["name"] == "白酒"
        wl.add_item("u_alice", "000858", "五粮液", group_id=g["id"])
        groups = wl.list_groups("u_alice")
        assert len(groups) == 2
        liquor = next(x for x in groups if x["name"] == "白酒")
        assert [i["code"] for i in liquor["items"]] == ["000858"]

        # 删除分组 → 组内自选一并删除
        assert wl.delete_group("u_alice", g["id"])
        groups = wl.list_groups("u_alice")
        assert len(groups) == 1

    def test_duplicate_group_raises(self, wl):
        wl.list_groups("u_alice")  # 建默认组
        with pytest.raises(ValueError, match="已存在"):
            wl.add_group("u_alice", "自选")
        wl.add_group("u_alice", "白酒")
        with pytest.raises(ValueError, match="已存在"):
            wl.add_group("u_alice", "白酒")

    def test_remove_item(self, wl):
        wl.add_item("u_alice", "600519", "贵州茅台")
        groups = wl.list_groups("u_alice")
        item = groups[0]["items"][0]
        wl.remove_item("u_alice", item["id"])
        assert wl.list_groups("u_alice")[0]["items"] == []


@pytest.fixture
def wl_client(tmp_path):
    """注入临时 db, 直接调用批量入自选接口函数 (无需 starlette TestClient)"""
    from app import main
    from app.services import watchlist as wlmod
    wlmod.watchlist_svc._db_path = tmp_path / "watchlist.db"
    wlmod.watchlist_svc._init_db()
    return main


class TestWatchlistBatch:
    """板块成分股多选/全选 → 批量加入自选 / 其他分类"""

    def test_batch_dedup_and_skip_empty(self, wl_client):
        m = wl_client
        d = m.watchlist_add_items(
            m.WatchItemsRequest(group_id=None, items=[
                {"code": "600519", "name": "贵州茅台"},
                {"code": "600519", "name": "贵州茅台"},
                {"code": "", "name": "空"},
            ]), user_id="u_batch")
        assert d == {"total": 3, "added": 1, "skipped": 2}

    def test_batch_into_custom_group(self, wl_client):
        from app.services import watchlist as wlmod
        m = wl_client
        g = wlmod.watchlist_svc.add_group("u_batch", "白酒")
        d = m.watchlist_add_items(
            m.WatchItemsRequest(group_id=g["id"], items=[
                {"code": "000858", "name": "五粮液"},
                {"code": "000568", "name": "泸州老窖"},
            ]), user_id="u_batch")
        assert d["added"] == 2
        groups = wlmod.watchlist_svc.list_groups("u_batch")
        liquor = next(x for x in groups if x["name"] == "白酒")
        assert sorted(i["code"] for i in liquor["items"]) == ["000568", "000858"]

    def test_batch_existing_counted_as_skipped(self, wl_client):
        from app.services import watchlist as wlmod
        m = wl_client
        wlmod.watchlist_svc.add_item("u_batch", "600519", "贵州茅台")
        d = m.watchlist_add_items(
            m.WatchItemsRequest(group_id=None, items=[{"code": "600519", "name": "贵州茅台"}]),
            user_id="u_batch")
        assert d == {"total": 1, "added": 0, "skipped": 1}


class TestGuestGating:
    """未登录(游客)门禁: 自选为空 / 复盘不展示 / 训练不可开启"""

    def test_guest_gets_empty_reads(self):
        from app import main as m
        assert m.watchlist_list(user_id="default") == {"groups": []}
        assert m.train_history(user_id="default") == {"sessions": []}
        assert m.strategy_stats(user_id="default") == {"stats": []}
        assert m.live_history(user_id="default") == {"sessions": []}
        eq = m.review_equity(user_id="default")
        assert eq["curve"] == [] and eq["can_start"] is True and eq["nav"] == 100.0

    def test_guest_history_detail_404(self):
        from app import main as m
        with pytest.raises(m.HTTPException) as e:
            m.train_history_detail("any-session", user_id="default")
        assert e.value.status_code == 404

    def test_require_user_id_rejects_guest(self):
        from app import main as m
        with pytest.raises(m.HTTPException) as e:
            m.require_user_id("")
        assert e.value.status_code == 401
        with pytest.raises(m.HTTPException) as e2:
            m.require_user_id("Bearer bogus-token")
        assert e2.value.status_code == 401

    def test_require_user_id_blocks_unbound_phone(self, tmp_path, monkeypatch):
        from app import main as m
        from app.services.auth import AuthService
        AuthService._instance = None
        a = AuthService()
        a._db_path = tmp_path / "auth.db"
        a._init_db()
        monkeypatch.setattr(m, "auth_svc", a)

        a.register("bound", "secret123", "13811110001")
        token = a.login("bound", "secret123")["token"]
        assert m.require_user_id("Bearer " + token) == a._find_user_name("bound")["user_id"]

        # 未绑手机号的账号 → 403 (直接造会话 token 指向无 phone 的账号)
        from sqlite3 import connect
        c = connect(str(tmp_path / "auth.db"))
        c.execute("INSERT INTO users (user_id, username, password_hash, salt, created_at) "
                  "VALUES ('u_un','unbound','h','s','2024-01-01')")
        c.execute("INSERT INTO sessions (token, user_id, expires_at) "
                  "VALUES ('tok_un','u_un','2999-01-01 00:00:00')")
        c.commit(); c.close()
        with pytest.raises(m.HTTPException) as e:
            m.require_user_id("Bearer tok_un")
        assert e.value.status_code == 403


class TestImportTokenCompat:
    """导入兼容格式 (A20): 市场标记归一化 + JSON 数组 + CSV/TSV 表头"""

    def test_normalize_market_variants(self):
        from app.main import _normalize_token
        assert _normalize_token("sh600519") == "600519"
        assert _normalize_token("SZ000001") == "000001"
        assert _normalize_token("600519.SH") == "600519"
        assert _normalize_token("SH.600519") == "600519"
        assert _normalize_token("00700.HK") == "00700"
        assert _normalize_token("'600519'") == "600519"
        assert _normalize_token("AAPL") == "AAPL"

    def test_extract_json_array(self):
        from app.main import _extract_import_tokens
        assert _extract_import_tokens('[{"code":"600519","name":"贵州茅台"}]') == ["600519"]
        assert _extract_import_tokens('["600519", "000001"]') == ["600519", "000001"]

    def test_extract_csv_keeps_field_order(self):
        from app.main import _extract_import_tokens, _HEADER_RE
        toks = _extract_import_tokens("代码,名称\n600519,贵州茅台\n000001,平安银行")
        assert toks == ["代码", "名称", "600519", "贵州茅台", "000001", "平安银行"]
        assert _HEADER_RE.match(toks[0])      # 首行表头可被识别并跳过
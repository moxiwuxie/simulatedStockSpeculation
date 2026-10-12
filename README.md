# 历史回放交易系统 (simulatedStockSpeculation)

一个「历史真实行情回放 + 训练 + 复盘」的模拟炒股系统。前后端分离：后端 FastAPI 提供行情/回放/撮合/训练/账户/板块/搜索等能力，前端 Vue3 + ECharts 负责交互与可视化。

- 后端 API 文档: http://localhost:8010/docs
- 前端页面: http://localhost:5501

---

## 一、快速开始

```bash
# 1. 后端依赖 (首次)
cd backend
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 2. 一键启动 (项目根目录): 同时拉起后端 8010 + 前端 5501 + 打开浏览器
cd ..
python run.py

# 其它启动方式
python run.py backend       # 只启动后端
python run.py frontend      # 只启动前端
python run.py --no-browser  # 不自动打开浏览器
```

手动分别启动（开发调试推荐，后端带热重载）：

```bash
cd backend  && source .venv/bin/activate && python3 -m uvicorn app.main:app --reload --port 8010
python3 proxy_server.py 5501        # 前端静态页 + /api 反向代理到后端 8010
```

> 前端经反向代理访问后端：浏览器只访问 `http://localhost:5501`，其中的 `/api/*` 由
> [proxy_server.py](proxy_server.py) 转发到后端（默认 `127.0.0.1:8010`）。因此前端代码里
> API 地址是相对路径 `/api`，**不要**再用 `python -m http.server` 直接起前端（那样 `/api` 会 404）。
> 后端地址可用环境变量覆盖：`BACKEND_HOST=... BACKEND_PORT=... python3 proxy_server.py 5501`。

运行测试：

```bash
cd backend && source .venv/bin/activate && python3 -m pytest tests/ -v
```

> 修改前端静态资源后，请把 [index.html](frontend/index.html) 里的 `?v=N` 版本号 +1，避免浏览器缓存旧脚本。

---

## 二、目录结构

```
simulatedStockSpeculation/
├── run.py                     # 一键启动器 (后端 uvicorn + 前端反代服务)
├── proxy_server.py            # 前端静态服务 + /api 反向代理 (标准库实现, 单端口 5501)
├── .gitignore                 # 忽略 venv / __pycache__ / *.db / .env 等
├── README.md                  # 本文件
├── backend/
│   ├── requirements.txt       # 后端依赖 (含版本)
│   ├── app/
│   │   ├── main.py            # ★ FastAPI 应用入口: 全部 HTTP 路由 (行情/交易/训练/账户/板块/搜索/资讯)
│   │   ├── models.py          # 领域模型: KLine / Stock / Order / Position / Account / OrderSide...
│   │   ├── core/config.py     # 全局配置: 资金分级 / 费率 / 涨跌停 / 交易时段
│   │   ├── data/
│   │   │   ├── stock_registry.py   # 全 A 股注册表 (SQLite) + 拼音检索
│   │   │   └── sample_stocks.py    # 内置样例标的
│   │   └── services/          # 业务服务层 (每个文件一个职责)
│   └── tests/                 # pytest: 训练会话 / 账户 / 自选
└── frontend/
    ├── index.html             # 页面骨架 (Vue 模板源, 极简壳)
    ├── style.css              # 全部样式
    ├── app.js                 # ★ Vue 应用: 行情/自选/板块/搜索/账户/资讯
    ├── train.js               # 训练 Tab 组件 (双盲 K 线训练)
    ├── review.js              # 复盘 Tab 组件 (战法统计/历史对局)
    ├── indicators.js          # 技术指标纯函数库 (SMA / MACD)
    └── vendor/                # 本地化的第三方库 (无外网依赖, 离线可用)
        ├── vue.global.js      #   Vue 3 full build (含 template compiler)
        └── echarts.min.js     #   ECharts 5
```

---

## 三、模块关系（结构化视图）

```
                        ┌─────────────────────────── 浏览器 ───────────────────────────┐
                        │  index.html (Vue3 full build + ECharts5)                     │
                        │   ├── app.js      行情 / 自选 / 板块 / 聚合搜索 / 账户 / 资讯 │
                        │   ├── train.js    训练组件 (双盲K线 · 逐根推进 · 买卖/观望)   │
                        │   ├── review.js   复盘组件 (战法库 · 统计 · 历史对局回看)     │
                        │   └── indicators.js  SMA / MACD 纯函数                       │
                        └───────────────────────────┬──────────────────────────────────┘
                                          HTTP/JSON │ (Authorization: Bearer <token>)
                        ┌───────────────────────────▼──────────────────────────────────┐
                        │              backend/app/main.py  (FastAPI 路由层)            │
                        └──┬────────┬────────┬────────┬────────┬────────┬───────────┬───┘
                           │        │        │        │        │        │           │
      ┌────────────────────▼─┐ ┌────▼─────┐ ┌▼────────┐ ┌▼───────┐ ┌▼───────┐ ┌─▼────────┐ ┌▼──────────┐
      │ replay_feed          │ │ market   │ │ intraday│ │ boards │ │matching│ │ account  │ │training_  │
      │ 日K拉取/缓存/周期聚合│ │ 实时行情 │ │ 分时(真 │ │ 板块   │ │ 撮合   │ │ 账户/    │ │session    │
      │ (新浪日线)           │ │          │ │ 实优先) │ │ 列表+  │ │ 引擎   │ │ 分级/    │ │ 训练会话  │
      └──────────┬───────────┘ └────┬─────┘ └────┬────┘ │ 成分股 │ └───┬────┘ │ 盈亏     │ │ + SQLite  │
                 │                  │            │      │ +指数  │     │      └────┬─────┘ └────┬──────┘
                 │                  │            │      └───┬────┘     │           │            │
      ┌──────────▼──────────────────▼────────────▼──────────▼──────────▼───────────▼────────────▼───────┐
      │ 支撑层: replay_engine (回放时钟/挂单结算) · fee (佣金/印花税/过户费) · ws_push (推送)               │
      │         auth (注册登录/PBKDF2/token) · watchlist (自选分组) · news (资讯) · stock_registry (注册表) │
      └──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 数据流（三条主链路）

1. **行情链路**：`index.html → app.js:loadKline/loadIntraday → /api/stocks/{code}/kline|intraday → replay_feed(日K) / intraday(分钟)`。
   板块行情走 `app.js:selectBoard → /api/boards/{label}/kline|intraday → boards.py(成分股复合指数)`。
2. **搜索链路**：`app.js:debounceSearch → /api/search?kw= → stock_registry.search(代码/名称/拼音) + boards.search_boards`。
3. **训练链路**：`train.js:start → /api/training/start → training_session.TrainingManager → SQLite(training_sessions/training_bars)`；
   结算后 `review.js:loadAll → /api/training/history + /api/strategies/stats` 读取同一份数据（同一账号视角）。

### 后端服务职责一览

| 模块 | 文件 | 职责 |
| --- | --- | --- |
| 路由层 | `main.py` | 所有 HTTP 接口、鉴权依赖 `get_current_user_id`、单例装配 |
| 回放数据 | `services/replay_feed.py` | 新浪日K 拉取/缓存、交易日历、周期(日/周/月/年)聚合、停牌判定 |
| 回放时钟 | `services/replay_engine.py` | 回放时钟推进、挂单转成交、日终结算 |
| 实时行情 | `services/market.py`、`services/realtime_feed.py` | 实时/回放行情快照与推送数据 |
| 分时 | `services/intraday.py` | 真实分钟优先，缺失时用 OHLC 布朗桥确定性重建 240 分钟 |
| 板块 | `services/boards.py` | 新浪板块列表 + 成分股；成分股复合指数(板块K线) + 加权板块分时 |
| 撮合 | `services/matching.py` | 订单簿撮合，买卖双方账户同时结算 |
| 费用 | `services/fee.py` | 佣金 / 印花税 / 过户费 |
| 账户 | `services/account.py` | 账户资金、持仓、分级(基础100万/进阶500万/专业1000万)、盈亏 |
| 账号 | `services/auth.py` | 注册/登录/PBKDF2 密码哈希/token 会话 |
| 自选 | `services/watchlist.py` | 自选股与自定义分组持久化 |
| 训练 | `services/training_session.py` | 训练会话生命周期、决策、结算、存档、战法统计 |
| 资讯 | `services/news.py` | 全球要闻/个股资讯(东财+新浪双源, 10 分钟缓存) |
| 推送 | `services/ws_push.py` | WebSocket 行情/状态推送 |
| 注册表 | `data/stock_registry.py` | 全 A 股列表(SQLite) + 拼音(首字母/全拼)检索 |

---

## 四、核心业务概念

- **回放时钟**：以后端 `replay.clock_date` 为准，行情随回放日期推进；历史 K 线不受时钟截断。
- **交易规则**：BUY 挂单冻结资金，T+1 开盘撮合；SELL 挂单锁定持仓，当日收盘撮合；含涨跌停与停牌限制。
- **训练**：独立训练资金池，每局重置；双盲模式下隐藏标的名称/代码/日期，结算后揭示。
  - 品类：股票双盲 / 涨停专项；周期支持 日/周/月/年 且运行中可切换。
  - 训练记录按账号隔离（`user_id`），结算/放弃/进行中的对局都会同步到复盘模块。
- **复盘**：战法库 + 分战法统计(胜率/盈亏比/最大回撤/爆仓) + 历史对局回看(权益曲线、买卖点) + 复盘笔记。
- **板块**：板块列表与成分股来自新浪(证监会行业分类 + 概念板块)；板块 K 线/分时为成分股复合指数（基准 1000）。
  受免费数据源限制：同花顺行业分类无成分股接口，故行业名称沿用新浪口径；分钟数据仅覆盖最近约 5 个交易日，更早日期分时为空数据样式。

---

## 五、接口速查

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/stocks/search` | 个股搜索(代码/名称/拼音) |
| GET | `/api/search` | 聚合搜索: 个股 + 板块 |
| GET | `/api/stocks/{code}` | 个股行情快照 |
| GET | `/api/stocks/{code}/kline` | 个股 K 线(day/week/month/year) |
| GET | `/api/stocks/{code}/intraday` | 个股分时(可指定 date) |
| GET | `/api/boards?kind=industry\|concept` | 板块列表 |
| GET | `/api/boards/{label}/stocks` | 板块成分股(按涨跌幅正序) |
| GET | `/api/boards/{label}/kline` | 板块 K 线(复合指数) |
| GET | `/api/boards/{label}/intraday` | 板块分时(加权合成) |
| GET | `/api/watchlist` | 自选分组 |
| POST | `/api/auth/register` / `/api/auth/login` | 注册 / 登录 |
| GET | `/api/account` / `/api/account/profit` | 账户资金 / 持仓盈亏 |
| POST | `/api/training/start` | 开始训练 |
| POST | `/api/training/{sid}/{buy,sell,hold,advance,period,settle}` | 训练决策 |
| GET | `/api/training/history` | 训练记录(按账号) |
| GET/PATCH | `/api/training/history/{sid}` | 单局详情 / 归类与笔记 |
| GET/POST/DELETE | `/api/strategies` | 战法库 |
| GET | `/api/strategies/stats` | 分战法统计 |

完整参数与响应见 Swagger：http://localhost:8010/docs

---

## 六、本地数据文件

运行时会生成 SQLite 文件（已在 `.gitignore` 中忽略，属本地状态、不入库）：

| 文件 | 内容 |
| --- | --- |
| `backend/app/data/trading.db` | 全 A 股注册表(含拼音列) |
| `backend/app/data/training.db` | 训练会话 / 每根K线决策 / 战法库 |
| `backend/app/data/auth.db` | 用户账号(密码经 PBKDF2 哈希) |
| `backend/app/data/watchlist.db` | 自选股分组 |

---

## 七、约束与已知限制

- 行情/板块/分钟数据源为免费公开接口（新浪稳定、东财已断连），首次拉取较慢，服务内做了 10 分钟内存缓存。
- 全 A 股 K 线为**按需懒加载 + 进程内缓存**，首次访问某只股票会明显偏慢。
- 训练记录、自选、账户均按 `user_id` 隔离；未登录时统一落在 `default`（游客）命名空间。

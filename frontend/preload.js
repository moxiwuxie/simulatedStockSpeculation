/**
 * 前端预加载 / 统一缓存层 (需求101602 · 需求101807-1)
 *
 * 目标: 各模块响应 > 0.5s 的接口实现「预加载」, 响应过长 / 数据过大的通过缓存处理。
 *
 * 提供:
 *   window.__apiFetch(path, opts, cfg)      —— 统一请求入口 (注入鉴权/错误处理) + 只读GET缓存 + 并发去重
 *   window.__apiCacheClear(prefix)          —— 手动失效缓存 (前缀匹配)
 *   window.__preload(urls)                  —— 注册预加载目标并启动「持续预加载」
 *   window.__preloadRegister(url, delay)    —— 新增单个预加载目标
 *   window.__preloadSnapshot()              —— 查看当前预加载任务/慢接口清单 (调试)
 *
 * 缓存策略: 仅对「只读市场数据」白名单接口启用 (见 ttlFor), 写操作/账户类接口一律不缓存,
 * 避免下单后读到过期持仓。同一 URL 的并发 GET 复用同一 Promise (single-flight)。
 *
 * 需求101807-1 持续预加载:
 *   - 每个预加载目标按「自身 TTL 的 80%」滚动排期, 上一轮请求结束后立即重置计时 → 缓存持续热;
 *   - 真实接口请求 (用户触发/组件自取) 结束后同样重置该目标的计时, 避免刚取完又立刻重复预热;
 *   - 运行时新发现的慢接口 (GET 且耗时 ≥ PRELOAD_SLOW_MS) 自动纳入预加载, 并写入 localStorage,
 *     下次进入系统即随默认清单一起预热。
 */
(function () {
  'use strict';

  var API_BASE = '/api';
  var __apiCache = new Map();      // url → { ts, ttl, data }
  var __apiInflight = new Map();   // url → Promise

  // 只读市场数据白名单 → TTL(ms); 返回 0 表示不缓存
  function ttlFor(path) {
    if (path === '/stocks' || path.indexOf('/stocks?') === 0) return 600000;   // 全量注册表 (大: ~776KB)
    if (path.indexOf('/stocks') === 0) return 30000;                            // 个股K线/分时/详情
    if (path.indexOf('/watch/panorama') === 0) return 15000;                    // 看盘全景 (实时快照)
    if (path.indexOf('/boards') === 0) return 60000;                            // 板块列表/K线/分时
    if (path.indexOf('/index') === 0) return 60000;                             // 指数列表/K线/分时
    if (path.indexOf('/search') === 0) return 300000;                           // 聚合搜索 (随注册表变化)
    if (path.indexOf('/watch/sparklines') === 0) return 60000;                 // 分时缩略图
    if (path.indexOf('/watchlist/sparklines') === 0) return 60000;              // 分时缩略图
    if (path.indexOf('/watch/list') === 0) return 60000;                        // 需求101807-0: 盘面「查看更多」榜单
    // 需求101605: 自选分组只读接口短缓存 (8s) —— 切换分组时直接命中前端缓存, 不再等待后端
    if (path === '/watchlist' || path.indexOf('/watchlist?') === 0) return 8000;
    if (path.indexOf('/watchlist/overview') === 0) return 8000;
    if (path.indexOf('/watchlist/quotes') === 0) return 8000;
    if (path === '/training/categories') return 600000;                         // 需求101603: 训练品类元数据 (全局)
    // 需求101701: 训练会话内只读分时(K线/背景) —— URL 带版本令牌(p=周期/b=推进根数/r=揭晓态),
    // 同一令牌下内容恒定, 可安全长缓存: 前后翻看历史K线分时命中缓存即刻展示。
    if (path.indexOf('/training/session/') === 0 &&
        (path.indexOf('/pre-intraday') > 0 || path.indexOf('/bar-intraday') > 0)) return 1800000;
    return 0;
  }

  function _pathOf(path) {
    if (path.indexOf('http') === 0) {
      try { var u = new URL(path); return u.pathname.replace(/^\/api/, '') + u.search; }
      catch (e) { return path; }
    }
    return path;
  }

  // ---------------- 需求101807-1: 持续预加载调度 ----------------
  var PRELOAD_MIN_MS = 15000;      // 单个目标最短排期间隔 (避免高频预热打满后端)
  var PRELOAD_SLOW_MS = 500;       // 「响应时间长」判定阈值 (需求101602: > 0.5s)
  var PRELOAD_STORE_KEY = 'preload_slow_v1';
  var PRELOAD_STORE_MAX = 24;      // 持久化的慢接口上限

  var __preloadJobs = new Map();   // url → timer (滚动排期)
  var __preloading = {};           // url → true (该 url 的预加载请求进行中)
  var __slowSeen = {};             // url → true (运行时发现的慢接口)

  function _delayFor(url) {
    // 按自身 TTL 的 80% 滚动: 缓存尚未过期时预热, 保证任何时刻都能命中热数据
    var ttl = ttlFor(_pathOf(url)) || 60000;
    return Math.max(PRELOAD_MIN_MS, Math.round(ttl * 0.8));
  }

  function _schedulePreload(url) {
    var job = __preloadJobs.get(url);
    if (job) clearTimeout(job);
    __preloadJobs.set(url, setTimeout(function () { _preloadOnce(url); }, _delayFor(url)));
  }

  function _preloadOnce(url) {
    __preloading[url] = true;
    var p;
    try {
      p = window.__apiFetch(url, { method: 'GET' }, { noStore: _pathOf(url) === '/preload' });
    } catch (e) { p = Promise.resolve(); }
    // 本轮请求结束 → 重置计时: 持续预加载 (需求101807-1)
    p.catch(function () {}).then(function () {
      delete __preloading[url];
      _schedulePreload(url);
    });
  }

  function _persistSlow(url) {
    try {
      var path = _pathOf(url);
      var list = JSON.parse(localStorage.getItem(PRELOAD_STORE_KEY) || '[]');
      if (list.indexOf(path) < 0) {
        list.unshift(path);
        localStorage.setItem(PRELOAD_STORE_KEY, JSON.stringify(list.slice(0, PRELOAD_STORE_MAX)));
      }
    } catch (e) { /* localStorage 不可用: 仅本次会话生效 */ }
  }

  /** 需求101807-1: 新增预加载目标 (慢接口自动纳入 / 业务显式注册) */
  window.__preloadRegister = function (url, delayMs) {
    var u = (url || '').indexOf('http') === 0 ? url : API_BASE + url;
    if (delayMs) {
      var job = __preloadJobs.get(u);
      if (job) clearTimeout(job);
      __preloadJobs.set(u, setTimeout(function () { _preloadOnce(u); }, delayMs));
    } else if (!__preloadJobs.has(u)) {
      _schedulePreload(u);
    }
    return u;
  };

  window.__preloadSnapshot = function () {
    return { jobs: Array.from(__preloadJobs.keys()), slow: Object.keys(__slowSeen) };
  };

  window.__apiFetch = function (path, opts, cfg) {
    cfg = cfg || {};
    var url = path.indexOf('http') === 0 ? path : API_BASE + path;
    var method = ((opts && opts.method) || 'GET').toUpperCase();
    var noStore = cfg.noStore || (opts && opts.cache === 'no-store');
    // 需求101701-2: cfg.httpNoStore —— 仍保留前端内存缓存收益, 但强制绕过浏览器 HTTP 缓存
    // (移动端曾缓存旧的品类列表, 导致看不到「长周期训练」)。
    var httpNoStore = !!cfg.httpNoStore;
    var ttl = ttlFor(_pathOf(path));
    var cacheable = method === 'GET' && !noStore && ttl > 0;

    if (cacheable) {
      var hit = __apiCache.get(url);
      if (hit && Date.now() - hit.ts < hit.ttl) return Promise.resolve(hit.data);
      if (__apiInflight.has(url)) return __apiInflight.get(url);
    }

    var p = (async function () {
      var headers = { 'Content-Type': 'application/json', ...((opts && opts.headers) || {}) };
      var token = localStorage.getItem('auth_token');
      if (token) headers['Authorization'] = 'Bearer ' + token;
      var fetchOpts = { ...(opts || {}), headers: headers };
      if (httpNoStore) fetchOpts.cache = 'no-store';
      var t0 = Date.now();                                   // 需求101807-1: 记录真实耗时
      var res = await fetch(url, fetchOpts);
      if (!res.ok) {
        if (res.status === 401) {
          localStorage.removeItem('auth_token');
          localStorage.removeItem('auth_user');
        }
        var t = await res.text();
        var msg = t;
        try { msg = JSON.parse(t).detail || t; } catch (e) {}
        throw new Error(msg || ('HTTP ' + res.status));
      }
      var ct = res.headers.get('content-type') || '';
      var data = ct.indexOf('json') >= 0 ? await res.json() : await res.text();
      var cost = Date.now() - t0;
      if (cacheable) __apiCache.set(url, { ts: Date.now(), ttl: ttl, data: data });
      if (cacheable && __preloadJobs.has(url)) {
        // 需求101807-1: 真实接口请求结束后重置预加载计时 (刚取到新数据, 无需立刻重复预热)
        // 预加载自身触发的请求由 _preloadOnce 统一重置, 这里跳过避免重复排期。
        if (!__preloading[url]) _schedulePreload(url);
      } else if (cacheable && cost >= PRELOAD_SLOW_MS) {
        // 需求101807-1: 新发现的慢接口自动纳入持续预加载 (并持久化, 下次启动即预热)
        __slowSeen[url] = true;
        _schedulePreload(url);
        _persistSlow(url);
      }
      // 需求101605: 写操作成功后自动失效同资源根的只读缓存 (避免读到过期自选列表)
      if (method !== 'GET') {
        var root = _pathOf(path).split('?')[0];
        // 需求101701: 训练会话内只读分时由 URL 版本令牌保证新鲜, 不随写操作整体失效
        if (root.indexOf('/training/session/') !== 0) {
          var seg = root.split('/')[1];
          if (seg) window.__apiCacheClear('/' + seg);
        }
      }
      return data;
    })();

    if (cacheable) {
      __apiInflight.set(url, p);
      p.then(function () { __apiInflight.delete(url); },
             function () { __apiInflight.delete(url); });
    }
    return p;
  };

  window.__apiCacheClear = function (prefix) {
    if (!prefix) { __apiCache.clear(); return; }
    Array.from(__apiCache.keys()).forEach(function (k) {
      if (k.indexOf(prefix) >= 0) __apiCache.delete(k);
    });
  };

  // 默认预加载清单: 覆盖冷启动 >0.5s 的重接口 (看盘全景 / 板块 / 指数分时 ...)
  var PRELOAD_TARGETS = [
    '/preload',                        // 触发后端后台预热
    '/watch/panorama',
    '/index/list',
    '/boards?kind=industry',
    '/boards?kind=concept',
    '/index/000001/kline?days=120',
    '/index/000001/intraday',
    '/training/categories',            // 需求101603: 训练「开始训练」设置面板依赖
  ];

  // 需求101807-1: 注册为「持续预加载」任务 (首次立即预热, 之后按 TTL 滚动排期)
  window.__preload = function (urls) {
    var list = (urls || PRELOAD_TARGETS).slice();
    // 上次运行发现的慢接口一并预热 (需求101807-1)
    try {
      var persisted = JSON.parse(localStorage.getItem(PRELOAD_STORE_KEY) || '[]');
      persisted.forEach(function (p) { if (list.indexOf(p) < 0) list.push(p); });
    } catch (e) { /* ignore */ }
    list.forEach(function (u) { window.__preloadRegister(u, 0); });
    // 首轮立即预热一轮, 随后由 _preloadOnce 自动滚动续期
    list.forEach(function (u) {
      var full = u.indexOf('http') === 0 ? u : API_BASE + u;
      _preloadOnce(full);
    });
  };

  // 页面加载后自动预加载一次 (市场数据无需登录; 尽早把后端缓存与前端缓存填热)
  function _auto() {
    if (document.readyState === 'complete') { window.__preload(); return; }
    window.addEventListener('load', function () { window.__preload(); });
  }
  _auto();
})();

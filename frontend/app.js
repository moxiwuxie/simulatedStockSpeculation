/**
 * 历史回放交易系统 —— 前端主逻辑
 *
 * 架构:
 *   index.html (极简模板壳)
 *   style.css  (所有样式)
 *   indicators.js (技术指标纯函数库)
 *   train.js   (训练 Tab 组件)
 *   review.js  (复盘 Tab 组件)
 *   app.js     (Vue createApp + setup + 组件注册) ← 本文件
 *
 * 设计原则:
 *   1. 所有 Tab 用 v-show (不用 v-if / :class 条件渲染)
 *      → DOM 始终存在 → ECharts 容器始终有非零尺寸
 *      → 彻底绕开 Vue fragment patch / removeFragment nextSibling null 问题
 *   2. ECharts init 只做一次 (在 selectStock 后), dispose 在下次 selectStock 或 tab 切换时
 *   3. 数据源单一: selectedStock → loadKline → klineChart; selectedKlineDate → loadIntraday → intradayChart
 *   4. Console 零错误作为硬指标
 */
const { createApp, ref, computed, watch, nextTick, onMounted, onUnmounted } = Vue;

// ============================================================
// API 封装
// ============================================================
// 相对路径: 由前端服务(proxy_server.py)将 /api 反向代理到后端, 前后端同源
const API_BASE = '/api';

// 分组下拉哨兵值: 「无」= 浏览未加入任何分组的股票 (与真实分组 id 不冲突)
const NONE_GROUP = '__none__';
// 动态分组下拉前缀: 与自选分组的数字 id 区隔
const DYN_PREFIX = 'dyn:';

// ============================================================
// 图表配色 (随主题动态刷新, ECharts option 构建时读取)
// ============================================================
const CHART = {
    up: '#ff4757', down: '#2ed573', brand: '#00b5ff', brand2: '#26a7ff',
    grid: '#1e2347', axisLine: '#2a2f4a', border2: '#3a4280', orange: '#ffaa00',
    dim: '#8892b0', axisLabel: '#888', axisLabel2: '#666', axisLabel3: '#555',
    sliderBg: 'rgba(38,42,77,0.6)', sliderFill: 'rgba(38,167,255,0.35)',
    sliderArea: 'rgba(58,66,128,0.4)', brandArea: 'rgba(0,181,255,0.1)',
};

function refreshChartColors() {
    const cs = getComputedStyle(document.documentElement);
    const g = (n, d) => (cs.getPropertyValue(n) || '').trim() || d;
    CHART.up = g('--up', CHART.up);
    CHART.down = g('--down', CHART.down);
    CHART.brand = g('--brand', CHART.brand);
    CHART.brand2 = g('--brand-2', CHART.brand2);
    CHART.grid = g('--border', CHART.grid);
    CHART.axisLine = g('--border-2', CHART.axisLine);
    CHART.border2 = g('--border-hover', CHART.border2);
    CHART.orange = g('--warn', CHART.orange);
    CHART.dim = g('--text-dim', CHART.dim);
    CHART.axisLabel = g('--text-dim', CHART.axisLabel);
    CHART.axisLabel2 = g('--text-mute', CHART.axisLabel2);
    CHART.axisLabel3 = g('--text-mute', CHART.axisLabel3);
    const light = document.documentElement.dataset.theme === 'light';
    CHART.sliderBg = light ? 'rgba(0,0,0,0.04)' : 'rgba(38,42,77,0.6)';
    CHART.sliderFill = light ? 'rgba(230,74,25,0.25)' : 'rgba(38,167,255,0.35)';
    CHART.sliderArea = light ? 'rgba(200,204,212,0.5)' : 'rgba(58,66,128,0.4)';
    CHART.brandArea = light ? 'rgba(230,74,25,0.10)' : 'rgba(0,181,255,0.1)';
}

async function api(path, opts = {}) {
    const url = path.startsWith('http') ? path : API_BASE + path;
    const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
    const token = localStorage.getItem('auth_token');
    if (token) headers['Authorization'] = 'Bearer ' + token;
    const res = await fetch(url, {
        headers,
        ...opts,
    });
    if (!res.ok) {
        if (res.status === 401) {
            localStorage.removeItem('auth_token');
            localStorage.removeItem('auth_user');
        }
        const text = await res.text();
        throw new Error(text || `HTTP ${res.status}`);
    }
    const ct = res.headers.get('content-type') || '';
    return ct.includes('json') ? res.json() : res.text();
}

// ============================================================
// 工具函数
// ============================================================
function formatAmount(n) {
    if (n == null) return '--';
    if (n >= 1e8) return (n / 1e8).toFixed(2) + '亿';
    if (n >= 1e4) return (n / 1e4).toFixed(2) + '万';
    return n.toLocaleString();
}
/** 成交额: 元 → 万/亿, 保留 2 位小数 + 单位 (金融行业标准) */
function fmtMoney(x) {
    if (x == null || isNaN(x)) return '--';
    const n = Number(x);
    if (Math.abs(n) >= 1e8) return (n / 1e8).toFixed(2) + '亿';
    if (Math.abs(n) >= 1e4) return (n / 1e4).toFixed(2) + '万';
    return n.toFixed(2) + '元';
}
/** 成交量: A股单位为「股」, 展示万/亿股; 美股/港股原样 */
function fmtVol(v) {
    if (v == null || isNaN(v)) return '--';
    const n = Number(v);
    if (Math.abs(n) >= 1e8) return (n / 1e8).toFixed(2) + '亿股';
    if (Math.abs(n) >= 1e4) return (n / 1e4).toFixed(2) + '万股';
    return n.toLocaleString() + '股';
}
function priceClass(pct) {
    if (pct > 0) return 'up';
    if (pct < 0) return 'down';
    return '';
}
function showToast(msg, type = 'info') {
    // 简易 toast: console + 页面临时提示
    console.log(`[toast:${type}] ${msg}`);
}

// ============================================================
// Vue App
// ============================================================
const app = createApp({
    // 不指定 template —— Vue full build 会自动用 mount 目标 (#app) 的 innerHTML 作为模板
    setup() {
        // ---------- 响应式状态 ----------
        const tabs = [
            { id: 'train', label: '训练', icon: '🎯', cls: 'train' },
            { id: 'live', label: '模拟交易', icon: '💹', cls: 'live' },
            { id: 'kline', label: '行情', icon: '📈', cls: 'kline' },
            { id: 'review', label: '复盘', icon: '📊', cls: '' },
        ];
        const activeTab = ref('kline');

        const searchKw = ref('');
        const searchResults = ref({ stocks: [], boards: [] });   // 聚合搜索结果 (个股 + 板块)
        const searchTimer = ref(null);

        const watchList = ref([]);       // 当前分组自选 code 列表 (后端持久化)
        const stocks = ref([]);          // 当前分组自选股带行情数据
        const watchGroups = ref([]);     // [{id, name, items: []}]
        const watchGroupId = ref(null);
        const newGroupName = ref('');
        const importFile = ref(null);    // 隐藏的文件选择框 (导入自选)
        let loadingWatch = false;    // 并发保护

        // ---------- 动态分组 (规则驱动, 每次实时获取) ----------
        const dynGroups = ref([]);       // [{id, name, rules, builtin}]
        const dynMetrics = ref([]);      // 指标元数据 (来自后端注册表)
        const dynLoading = ref(false);
        const dynSaving = ref(false);
        const dynEditorOpen = ref(false);
        const dynEditId = ref(null);     // null=新建; 否则为被编辑分组 id
        const dynEditBuiltin = ref(false);
        const dynName = ref('');
        const dynConds = ref([]);        // [{metric, op, value, value1, value2, boolVal}]
        const dynPreviewMsg = ref('');

        // ---------- 设置模块 / 主题 ----------
        const settingsOpen = ref(false);
        const settingsTab = ref('account');    // account | style | about
        const theme = ref(localStorage.getItem('theme') || 'dark');

        const selectedStock = ref(null);
        const selectedKlineDate = ref(null);

        const klineDataset = ref([]);    // 缓存 K 线数据 (click 时按 index 取 date)
        const intradayData = ref(null);
        const intradayLoading = ref(false);
        const intradayAmp = ref(null);   // 当前分时振幅 (%)

        // ---------- K线周期 ----------
        const klinePeriods = [
            { key: 'day', label: '日线' },
            { key: 'week', label: '周线' },
            { key: 'month', label: '月线' },
            { key: 'year', label: '年线' },
        ];
        const klinePeriod = ref('day');
        // K线均线周期与配色 (5/10/20/30/60/120/250)
        const MA_KEYS = [5, 10, 20, 30, 60, 120, 250];
        const MA_COLORS = {
            5: '#f0843f', 10: '#5b8ff9', 20: '#e8362f', 30: '#52c41a',
            60: '#a259ff', 120: '#ff7a9e', 250: '#d4b106',
        };

        // ---------- 账户 ----------
        const acct = ref({ tier: 'basic', cash: 0, frozen_cash: 0, total_assets: 0, positions_count: 0 });
        const tiers = ref([]);
        const profit = ref({ total_profit: 0, total_profit_pct: 0, positions: [] });

        // ---------- 账号体系 ----------
        const user = ref(null);                  // {user_id, username, phone, email} 或 null
        const authMode = ref('login');           // 'login' | 'register'
        const authUser = ref('');
        const authPass = ref('');
        const authPhone = ref('');
        const authEmail = ref('');
        const authShowPass = ref(false);         // 密码明文可见
        const authMsg = ref('');
        const authLoading = ref(false);
        const needBindPhone = ref(false);        // 旧账号未绑手机号 → 强制补绑后方可使用
        const bindPhoneSubmitting = ref(false);
        // 已登录且已绑手机号才视为「可正常使用」
        const isLoggedIn = computed(() => !!user.value && !needBindPhone.value);

        const PHONE_RE = /^1[3-9]\d{9}$/;
        const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
        function validPhone(v) { return PHONE_RE.test(String(v || '').trim()); }

        (function restoreSession() {
            const raw = localStorage.getItem('auth_user');
            if (raw) { try { user.value = JSON.parse(raw); } catch (e) {} }
        })();

        /** 拉取 /auth/me: 刷新手机号绑定状态 (登录态恢复后判断是否需要补绑) */
        async function refreshMe() {
            if (!localStorage.getItem('auth_token')) {
                user.value = null; needBindPhone.value = false; return;
            }
            try {
                const me = await api('/auth/me');
                if (!me.logged_in) { user.value = null; needBindPhone.value = false; return; }
                user.value = { user_id: me.user_id, username: me.username,
                               phone: me.phone || '', email: me.email || '' };
                localStorage.setItem('auth_user', JSON.stringify(user.value));
                needBindPhone.value = !me.phone_bound;
            } catch (e) {
                user.value = null; needBindPhone.value = false;   // 401 已在 api() 清理 token
            }
        }

        async function authSubmit() {
            authMsg.value = '';
            if (!authUser.value.trim() || !authPass.value) {
                authMsg.value = '请输入用户名和密码'; return;
            }
            if (authMode.value === 'register') {
                if (!authPhone.value.trim()) { authMsg.value = '请输入手机号'; return; }
                if (!validPhone(authPhone.value)) { authMsg.value = '手机号格式不正确'; return; }
                if (authEmail.value.trim() && !EMAIL_RE.test(authEmail.value.trim())) {
                    authMsg.value = '邮箱格式不正确'; return;
                }
            }
            authLoading.value = true;
            try {
                const path = authMode.value === 'login' ? '/auth/login' : '/auth/register';
                const body = { username: authUser.value.trim(), password: authPass.value };
                if (authMode.value === 'register') {
                    body.phone = authPhone.value.trim();
                    body.email = authEmail.value.trim();
                }
                const res = await api(path, { method: 'POST', body: JSON.stringify(body) });
                localStorage.setItem('auth_token', res.token);
                user.value = { user_id: res.user_id, username: res.username,
                               phone: res.phone || '', email: res.email || '' };
                localStorage.setItem('auth_user', JSON.stringify(user.value));
                needBindPhone.value = !!res.need_bind_phone;
                authPass.value = '';
                await refreshMe();          // 拉取 /auth/me 补齐 phone/email 与绑定状态(以服务端为准)
                authMsg.value = authMode.value === 'login' ? '登录成功' : '注册成功';
                if (!needBindPhone.value) { await loadAccount(); await refreshWatch(); await loadDynGroups(); }
            } catch (e) {
                let msg = e.message;
                try { msg = JSON.parse(e.message).reason || e.message; } catch (err) {}
                authMsg.value = '❌ ' + String(msg).replace(/^"|"$/g, '');
            } finally {
                authLoading.value = false;
            }
        }

        /** 旧账号补绑手机号 (绑定成功后方可正常使用) */
        async function submitBindPhone() {
            authMsg.value = '';
            if (!authPhone.value.trim()) { authMsg.value = '请输入手机号'; return; }
            if (!validPhone(authPhone.value)) { authMsg.value = '手机号格式不正确'; return; }
            if (authEmail.value.trim() && !EMAIL_RE.test(authEmail.value.trim())) {
                authMsg.value = '邮箱格式不正确'; return;
            }
            bindPhoneSubmitting.value = true;
            try {
                const p = await api('/auth/bind-phone', { method: 'POST', body: JSON.stringify({
                    phone: authPhone.value.trim(), email: authEmail.value.trim(),
                }) });
                user.value = { user_id: p.user_id, username: p.username,
                               phone: p.phone || '', email: p.email || '' };
                localStorage.setItem('auth_user', JSON.stringify(user.value));
                needBindPhone.value = false;
                authPhone.value = ''; authEmail.value = '';
                authMsg.value = '手机号绑定成功';
                await loadAccount();
                await refreshWatch();
                await loadDynGroups();
            } catch (e) {
                authMsg.value = '❌ ' + String(e.message).replace(/^"|"$/g, '');
            } finally {
                bindPhoneSubmitting.value = false;
            }
        }

        async function logout() {
            try {
                const token = localStorage.getItem('auth_token');
                if (token) await api('/auth/logout', { method: 'POST', body: '{}' });
            } catch (e) {}
            localStorage.removeItem('auth_token');
            localStorage.removeItem('auth_user');
            user.value = null;
            needBindPhone.value = false;
            authUser.value = ''; authPass.value = ''; authPhone.value = ''; authEmail.value = '';
            authMsg.value = '';
            dynGroups.value = [];                                  // 动态分组为账号数据, 登出后清空
            if (isDynSelection.value) watchGroupId.value = null;    // 复位到默认自选分组
            await refreshWatch();          // 未登录 → 自选为空
        }

        async function loadAccount() {
            try {
                acct.value = await api('/account');
                const t = await api('/account/tiers');
                tiers.value = t.tiers || [];
                const p = await api('/account/profit');
                profit.value = p;
            } catch (e) {
                console.error('账户加载失败:', e.message);
            }
        }

        async function setTier(tier) {
            if (acct.value.tier === tier) return;
            try {
                await api('/account/tier/' + tier, { method: 'POST', body: '{}' });
                await loadAccount();
            } catch (e) { console.error('切换分级失败:', e.message); }
        }

        async function resetAccount() {
            try {
                await api('/account/reset', { method: 'POST', body: '{}' });
                await loadAccount();
            } catch (e) { console.error('重置失败:', e.message); }
        }

        // ---------- ECharts 实例 (模块级, 因为 dispose 需要) ----------
        let klineChart = null;
        let intradayChart = null;
        // K 线可视窗口缓存 (索引口径), 供 wheel 分流与重绘保持窗口一致
        const klineCache = { zStart: null, zEnd: null };

        function disposeCharts() {
            try { klineChart?.dispose(); } catch(e) {}
            try { intradayChart?.dispose(); } catch(e) {}
            klineChart = null;
            intradayChart = null;
        }

        // ---------- Tab 切换 ----------
        function switchTab(tabId) {
            activeTab.value = tabId;
            // v-show 不需要 DOM 操作, 只需 resize ECharts
            nextTick(() => {
                if (tabId === 'kline') { klineChart?.resize(); intradayChart?.resize(); }
                if (tabId === 'account') loadAccount();
            });
        }

        // ---------- 周期切换 ----------
        async function setPeriod(key) {
            if (klinePeriod.value === key) return;
            klinePeriod.value = key;
            selectedKlineDate.value = null;
            await loadKline();
            linkIntradayToSelected();
        }
        // 分时联动到指定索引 (缺省用当前选中日期或最新一根)
        function linkIntradayToSelected(idx = -1) {
            const data = klineDataset.value;
            if (!data?.length) return;
            const bar = idx >= 0 ? data[idx] : data.find(d => d.date === selectedKlineDate.value) || data[data.length - 1];
            if (bar) loadIntraday(bar.date);
        }

        // ---------- 搜索 (debounce, 聚合个股 + 板块, 支持代码/名称/首字母/模糊) ----------
        function debounceSearch() {
            if (searchTimer.value) clearTimeout(searchTimer.value);
            const kw = searchKw.value.trim();
            if (!kw) { searchResults.value = { stocks: [], boards: [] }; return; }
            searchTimer.value = setTimeout(async () => {
                try {
                    searchResults.value = await api('/search?kw=' + encodeURIComponent(kw));
                } catch(e) {
                    console.error('搜索失败:', e.message);
                }
            }, 250);
        }

        // ---------- 从搜索结果选中 → 加入自选(当前分组) + 选股票 ----------
        async function selectFromSearch(s) {
            try {
                await api('/watchlist/item', { method: 'POST', body: JSON.stringify({
                    group_id: currentGroupId(), code: s.code, name: s.name,
                }) });
            } catch(e) { console.warn('加入自选失败:', e.message); }
            searchKw.value = '';
            searchResults.value = { stocks: [], boards: [] };
            // 刷新自选股行情 → 再 selectStock
            await refreshWatch();
            const fresh = stocks.value.find(x => x.code === s.code);
            selectStock(fresh || { ...s, price: 0 });
        }

        // ---------- 从搜索结果选中板块 → 打开板块面板并展示板块行情 ----------
        async function selectBoardFromSearch(b) {
            searchKw.value = '';
            searchResults.value = { stocks: [], boards: [] };
            boardOpen.value = true;
            if (boardKind.value !== b.kind || !boards.value.length) {
                boardKind.value = b.kind;
                await loadBoards();
            }
            selectBoard(b, b.kind);
        }

        // ---------- 板块查询 (行业/概念) ----------
        const boardOpen = ref(false);
        const boardKind = ref('industry');
        const boards = ref([]);
        const boardLoading = ref(false);
        const boardName = ref('');
        const boardStocks = ref([]);
        const boardConsLoading = ref(false);
        const boardStats = ref(null);      // 板块指数最新一根 K 线 (头部展示)
        const boardSortedAsc = ref(false);  // 成分股默认按涨跌幅从大到小(降序)
        // 板块面板内搜索 (同花顺风格: 一次搜「行业 + 概念」)
        const boardKw = ref('');
        const boardSearching = ref(false);
        const boardSearchResults = ref([]);
        let boardSearchTimer = null;
        const boardList = computed(() =>
            boardKw.value.trim() ? boardSearchResults.value : boards.value);
        const boardListCount = computed(() => boardList.value.length);
        // 成分股多选/全选 → 批量加入自选(或指定分类)
        const boardSel = ref([]);            // 选中 code 数组
        const boardAddGroupId = ref(null);   // 目标分组 (null = 默认分组)
        const boardAdding = ref(false);
        const boardAllSel = computed(() =>
            boardStocks.value.length > 0 && boardSel.value.length === boardStocks.value.length);
        function toggleBoardSel(code) {
            const i = boardSel.value.indexOf(code);
            if (i >= 0) boardSel.value.splice(i, 1);
            else boardSel.value.push(code);
        }
        function toggleBoardAll(checked) {
            boardSel.value = checked ? boardStocks.value.map(s => s.code) : [];
        }
        async function batchAddBoardStocks() {
            if (!boardSel.value.length) return;
            boardAdding.value = true;
            try {
                const items = boardStocks.value
                    .filter(s => boardSel.value.includes(s.code))
                    .map(s => ({ code: s.code, name: s.name }));
                const res = await api('/watchlist/items', { method: 'POST', body: JSON.stringify({
                    group_id: boardAddGroupId.value === NONE_GROUP ? null : boardAddGroupId.value,
                    items,
                }) });
                await refreshWatch();
                boardSel.value = [];
                console.log(`板块批量加入自选: 新增 ${res.added} 只, 跳过 ${res.skipped} 只`);
            } catch (e) { console.error('批量加入自选失败:', e.message); }
            finally { boardAdding.value = false; }
        }

        function debounceBoardSearch() {
            if (boardSearchTimer) clearTimeout(boardSearchTimer);
            const kw = boardKw.value.trim();
            if (!kw) { boardSearchResults.value = []; boardSearching.value = false; return; }
            boardSearching.value = true;
            boardSearchTimer = setTimeout(async () => {
                try {
                    boardSearchResults.value =
                        (await api('/boards/search?kw=' + encodeURIComponent(kw))).boards || [];
                } catch (e) {
                    console.error('板块搜索失败:', e.message);
                    boardSearchResults.value = [];
                } finally { boardSearching.value = false; }
            }, 250);
        }
        function clearBoardSearch() {
            boardKw.value = '';
            boardSearchResults.value = [];
            boardSearching.value = false;
        }

        function toggleBoard() {
            boardOpen.value = !boardOpen.value;
            if (boardOpen.value && !boards.value.length) loadBoards();
        }
        function switchBoardKind(k) {
            if (boardKind.value === k) return;
            boardKind.value = k;
            boardName.value = '';
            boardStocks.value = [];
            if (selectedStock.value?.isBoard) selectedStock.value = null;   // 退出板块视图
            loadBoards();
        }
        async function loadBoards() {
            boardLoading.value = true;
            try {
                boards.value = (await api('/boards?kind=' + boardKind.value)).boards || [];
            } catch(e) { console.error('板块加载失败:', e.message); boards.value = []; }
            finally { boardLoading.value = false; }
        }
        /** 选择板块: 加载成分股(按涨跌幅正序) + 板块K线/分时 (对标同花顺板块行情) */
        async function selectBoard(b, kind) {
            const k = kind || boardKind.value;
            boardKind.value = k;
            boardName.value = b.name;
            boardSel.value = [];        // 切换板块清空多选, 避免跨板块残留
            boardConsLoading.value = true;
            try {
                // 新浪板块详情需要唯一标识 label (如 hangye_ZA01), 而非中文名称
                boardStocks.value = (await api('/boards/' + encodeURIComponent(b.label) + '/stocks?kind=' + k)).stocks || [];
                applyBoardSort();   // 接口固定返回正序, 按当前排序方向重排
            } catch(e) { console.error('成分股加载失败:', e.message); boardStocks.value = []; }
            finally { boardConsLoading.value = false; }
            selectStock({ code: 'BOARD', label: b.label, boardKind: k, name: b.name,
                          isBoard: true, price: b.pct, pct: b.pct });
        }
        /** 按当前方向对成分股排序 (升序/降序) */
        function applyBoardSort() {
            boardStocks.value = boardStocks.value.slice().sort((a, x) => {
                const d = (a.pct ?? 0) - (x.pct ?? 0);
                return boardSortedAsc.value ? d : -d;
            });
        }
        /** 成分股排序切换 (正序/倒序) */
        function toggleBoardSort() {
            boardSortedAsc.value = !boardSortedAsc.value;
            applyBoardSort();
        }
        // 板块成分股 → 股票 (6/9开头→沪, 0/3→深, 92/4/8→北交所)
        function marketOf(code) {
            if (code.startsWith('92') || code.startsWith('4') || code.startsWith('8')) return 'bj';
            return (code.startsWith('6') || code.startsWith('9')) ? 'sh' : 'sz';
        }
        function selectBoardStock(s) {
            selectStock({ code: s.code, name: s.name, price: s.price || 0, change_pct: s.pct, market: marketOf(s.code) });
        }
        // 板块面板未开启时的自动加载 (首次展开 lazily)

        async function refreshWatch() {
            if (loadingWatch) return;
            loadingWatch = true;
            try {
                const g = await api('/watchlist');
                watchGroups.value = g.groups || [];
                // 动态分组视图: 只需刷新分组下拉, 个股由动态规则实时求值
                if (isDynSelection.value) return;
                // 「无」视图: 只刷新分组下拉, stocks 由 loadUngrouped() 填充
                if (watchGroupId.value === NONE_GROUP) return;
                if (!watchGroups.value.length) { stocks.value = []; return; }
                if (!watchGroups.value.find(x => x.id === watchGroupId.value)) {
                    watchGroupId.value = watchGroups.value[0].id;
                }
                const grp = watchGroups.value.find(x => x.id === watchGroupId.value) || watchGroups.value[0];
                watchList.value = grp.items;
                if (!watchList.value.length) { stocks.value = []; return; }
                const results = [];
                for (const s of watchList.value) {
                    try {
                        results.push(await api('/stocks/' + s.code));
                    } catch(e) {
                        console.warn('自选股行情失败', s.code, e.message);
                        results.push({ code: s.code, name: s.name, price: 0 });
                    }
                }
                stocks.value = results;
                // 如果已选中的股票有了新行情, 更新
                if (selectedStock.value) {
                    const fresh = results.find(x => x.code === selectedStock.value.code);
                    if (fresh) selectedStock.value = fresh;
                }
            } catch(e) {
                console.warn('自选分组加载失败:', e.message);
            } finally {
                loadingWatch = false;
            }
        }

        /** 当前分组用于「加入自选」的 group_id: 「无」视图回落到默认分组(null) */
        function currentGroupId() {
            return watchGroupId.value === NONE_GROUP ? null : watchGroupId.value;
        }

        /** 左侧列表标题: 跟随分类选择 (动态分组 / 「无」/ 分组名) */
        const watchTitle = computed(() => {
            if (isDynSelection.value) {
                const g = dynGroups.value.find(x => DYN_PREFIX + x.id === watchGroupId.value);
                return '⚡ ' + (g ? g.name : '动态分组');
            }
            if (watchGroupId.value === NONE_GROUP) return '📋 无';
            const g = watchGroups.value.find(x => x.id === watchGroupId.value);
            return '📋 ' + (g ? g.name : '自选股');
        });

        /** 按当前下拉选择刷新列表: 动态分组→按指标实时获取, 「无」→未分组, 其他→自选分组 */
        async function reloadWatchView(force = false) {
            if (isDynSelection.value) await loadDynStocks(currentDynGroupId.value, force);
            else if (watchGroupId.value === NONE_GROUP) await loadUngrouped();
            else await refreshWatch();
        }

        /** 加载「未加入任何分组」的股票 (注册表中有、且不在用户任一自选分组) */
        async function loadUngrouped() {
            try {
                const r = await api('/watchlist/ungrouped?limit=300');
                stocks.value = r.items || [];
            } catch(e) {
                console.warn('未分组股票加载失败:', e.message);
                stocks.value = [];
            }
        }

        async function switchGroup(id) {
            watchGroupId.value = id;
            await reloadWatchView();
        }

        // ---------- 导入自选 (txt/csv) ----------
        function triggerImport() {
            importFile.value && importFile.value.click();
        }

        function onImportFile(e) {
            const file = e.target.files && e.target.files[0];
            if (!file) return;
            const reader = new FileReader();
            reader.onload = async () => {
                try {
                    const res = await api('/watchlist/import', { method: 'POST', body: JSON.stringify({
                        content: String(reader.result || ''), group_id: currentGroupId(),
                    }) });
                    alert(`成功导入 ${res.added} 只，跳过 ${res.skipped} 只`);
                } catch(err) {
                    alert('导入失败: ' + err.message);
                } finally {
                    e.target.value = '';          // 允许再次选择同一文件
                    await reloadWatchView();
                }
            };
            reader.readAsText(file);
        }

        /** 仅刷新当前分组自选股的最新价/涨跌幅 (就地更新, 不重排列表) */
        async function refreshQuotes() {
            if (isDynSelection.value) {                 // 动态分组: 每次按指标重新获取
                await loadDynStocks(currentDynGroupId.value, true);
                return;
            }
            if (!stocks.value.length) return;
            if (watchGroupId.value === NONE_GROUP) return;   // 「无」视图为浏览列表, 不轮询
            const updated = [];
            for (const s of stocks.value) {
                if (s.isBoard) { updated.push(s); continue; }
                try {
                    const fresh = await api('/stocks/' + s.code);
                    updated.push({ ...s, ...fresh });
                } catch(e) { updated.push(s); }
            }
            stocks.value = updated;
            if (selectedStock.value && !selectedStock.value.isBoard) {
                const match = updated.find(x => x.code === selectedStock.value.code);
                if (match) selectedStock.value = { ...selectedStock.value, ...match };
            }
        }

        async function addGroup() {
            const name = newGroupName.value.trim();
            if (!name) return;
            try {
                const g = await api('/watchlist/group', { method: 'POST', body: JSON.stringify({ name }) });
                watchGroupId.value = g.id;
            } catch(e) { console.warn('新建分组失败:', e.message); }
            newGroupName.value = '';
            await refreshWatch();
        }

        async function delGroup() {
            if (watchGroupId.value == null) return;
            if (isDynSelection.value) {                 // 动态分组走独立删除
                await deleteDynGroup(currentDynGroupId.value);
                return;
            }
            if (watchGroupId.value === NONE_GROUP) return;   // 「无」为浏览视图, 不可删
            try {
                await api('/watchlist/group/' + watchGroupId.value, { method: 'DELETE' });
            } catch(e) { console.warn('删除分组失败:', e.message); }
            watchGroupId.value = null;
            await refreshWatch();
        }

        async function removeWatch(s) {
            const grp = watchGroups.value.find(x => x.id === watchGroupId.value);
            const item = grp && grp.items.find(i => i.code === s.code);
            if (!item) return;
            try { await api('/watchlist/item/' + item.id, { method: 'DELETE' }); } catch(e) {}
            await refreshWatch();
        }

        // ---------- 选股票/板块 (核心入口) ----------
        function selectStock(s) {
            if (!s || !s.code) return;
            selectedStock.value = s;
            selectedKlineDate.value = null;
            intradayData.value = null;
            intradayAmp.value = null;
            if (!s.isBoard) boardStats.value = null;   // 切回个股时清掉板块指数展示

            // 切到 K 线 tab (如果不在)
            if (activeTab.value !== 'kline') switchTab('kline');

            // nextTick 后 init ECharts (此时 K 线 tab 已经 v-show 为 visible)
            nextTick(async () => {
                await loadKline();
                await loadIntraday(null);
            });
        }

        // ---------- 加载 K 线 (个股 / 板块复合指数) ----------
        async function loadKline() {
            const sb = selectedStock.value;
            if (!sb?.code) return;
            try {
                const data = sb.isBoard
                    ? await api(`/boards/${encodeURIComponent(sb.label)}/kline?kind=${sb.boardKind}&days=1000&period=${klinePeriod.value}`)
                    : await api(`/stocks/${sb.code}/kline?days=1000&period=${klinePeriod.value}`);
                klineDataset.value = data || [];
                boardStats.value = (sb.isBoard && data?.length) ? data[data.length - 1] : null;

                // dispose 旧实例 + init 新的
                try { klineChart?.dispose(); } catch(e) {}
                klineChart = null;

                // waitForEl: 等容器有非零尺寸
                await waitForSize('kline-chart');
                klineChart = echarts.init(document.getElementById('kline-chart'));
                klineCache.zStart = null; klineCache.zEnd = null;   // 新标的/新周期: 重置窗口缓存
                drawKline(data);
                bindKlineClick();
                bindKlineWheel();
            } catch(e) {
                console.error('loadKline 失败:', e.message);
            }
        }

        // ---------- 加载分时 (个股 / 板块) ----------
        async function loadIntraday(date = null) {
            const sb = selectedStock.value;
            if (!sb?.code) return;
            intradayLoading.value = true;
            intradayData.value = null;
            try {
                const url = sb.isBoard
                    ? `/boards/${encodeURIComponent(sb.label)}/intraday?kind=${sb.boardKind}`
                      + (date ? `&date=${encodeURIComponent(date)}` : '')
                    : (date
                        ? `/stocks/${sb.code}/intraday?date=${encodeURIComponent(date)}`
                        : `/stocks/${sb.code}/intraday`);
                const data = await api(url);
                intradayData.value = data;
                intradayAmp.value = (data?.amplitude != null) ? data.amplitude
                    : (data?.high != null && data?.low != null && data?.prev_close)
                        ? Number(((data.high - data.low) / data.prev_close * 100).toFixed(2)) : null;
                if (data?.suspended || data?.empty || !data?.points?.length) {
                    // 停牌 / 无真实分钟数据: 不画 canvas, 由遮罩层展示提示
                    try { intradayChart?.dispose(); } catch(e) {}
                    intradayChart = null;
                    return;
                }
                try { intradayChart?.dispose(); } catch(e) {}
                intradayChart = null;
                await waitForSize('intraday-chart');
                intradayChart = echarts.init(document.getElementById('intraday-chart'));
                drawIntraday(data);
            } catch(e) {
                console.error('loadIntraday 失败:', e.message);
            } finally {
                intradayLoading.value = false;
            }
        }

        // ---------- waitForSize: 等容器有非零尺寸 (带 fallback) ----------
        function waitForSize(id, timeout = 2000) {
            return new Promise((resolve) => {
                const start = Date.now();
                function tick() {
                    const el = document.getElementById(id);
                    if (el) {
                        const r = el.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0) { resolve(el); return; }
                    }
                    if (Date.now() - start > timeout) {
                        // fallback: 就算超时也 resolve (让 ECharts 自己处理)
                        resolve(el || document.getElementById(id));
                        return;
                    }
                    requestAnimationFrame(tick);
                }
                tick();
            });
        }

        // ---------- 绘制 K 线 ----------
        function drawKline(data, keepZoom) {
            if (!klineChart || !data?.length) return;
            const dates = data.map(d => d.date);
            const ohlc = data.map(d => [d.open, d.close, d.low, d.high]);
            const vols = data.map(d => d.volume);
            const closes = data.map(d => d.close);
            const total = dates.length;
            // 可视窗口: 默认展示最新 120 根; keepZoom 时优先复用缓存窗口,
            // 其次读 startValue/endValue, 再退化为 start/end 百分比 (缩放后仍能保持, 不重置)
            let zStart = Math.max(0, total - 120), zEnd = total - 1;
            if (klineCache.zStart != null && klineCache.zEnd != null && total -
                (klineCache.zEnd - klineCache.zStart + 1) >= 0) {
                zStart = Math.min(klineCache.zStart, total - 1);
                zEnd = Math.min(klineCache.zEnd, total - 1);
            }
            if (keepZoom && klineChart) {
                try {
                    const dz = klineChart.getOption().dataZoom[0];
                    if (dz.startValue != null && dz.endValue != null) {
                        zStart = dz.startValue; zEnd = dz.endValue;
                    } else if (dz.start != null && dz.end != null) {
                        zStart = Math.round(dz.start / 100 * (total - 1));
                        zEnd = Math.round(dz.end / 100 * (total - 1));
                    }
                } catch (e) {}
            }
            klineCache.zStart = zStart; klineCache.zEnd = zEnd;

            // 均线 5/10/20/30/60/120/250 (数据不足时 null 自动跳过)
            const maSeries = [];
            if (window.Indicators?.sma) {
                MA_KEYS.forEach(n => {
                    maSeries.push({
                        name: 'MA' + n, type: 'line', data: Indicators.sma(closes, n),
                        xAxisIndex: 0, yAxisIndex: 0, showSymbol: false, smooth: true,
                        lineStyle: { width: 1, color: MA_COLORS[n] },
                        itemStyle: { color: MA_COLORS[n] },
                    });
                });
            }

            // 选中K线高亮: 保留在 K线 series 上, 避免整体替换导致图表消失
            const kSeries = {
                name: 'K线', type: 'candlestick', data: ohlc,
                itemStyle: { color: CHART.up, color0: CHART.down, borderColor: CHART.up, borderColor0: CHART.down },
                xAxisIndex: 0, yAxisIndex: 0,
                emphasis: { focus: 'series' },
            };
            if (selectedKlineDate.value) {
                kSeries.markLine = {
                    symbol: 'none', silent: true,
                    lineStyle: { color: CHART.brand, type: 'dashed', width: 1 },
                    label: { show: true, color: CHART.brand, fontSize: 10, position: 'start',
                             formatter: selectedKlineDate.value.slice(5) },
                    data: [{ xAxis: selectedKlineDate.value }],
                };
            }

            klineChart.setOption({
                backgroundColor: 'transparent',
                tooltip: {
                    trigger: 'axis', axisPointer: { type: 'cross' },
                    formatter: (params) => {
                        const i = params[0].dataIndex;
                        const k = data[i];
                        const chg = k.change_pct != null ? k.change_pct.toFixed(2)
                                   : (i > 0 ? ((k.close - data[i-1].close) / data[i-1].close * 100).toFixed(2) : '--');
                        return `<b>${k.date}</b><br/>开 ${k.open} 收 ${k.close} (${chg}%)<br/>高 ${k.high} 低 ${k.low}<br/>量 ${formatAmount(k.volume)}`;
                    },
                },
                dataZoom: [
                    {
                        type: 'inside', xAxisIndex: [0, 1, 2],
                        startValue: zStart, endValue: zEnd,
                        // 关闭内置滚轮缩放/平移: 纵向滚动交还页面, 横向/捏合由容器级 wheel 分流处理
                        zoomOnMouseWheel: false, moveOnMouseWheel: false, moveOnMouseMove: true,
                    },
                    {
                        type: 'slider', xAxisIndex: [0, 1, 2],
                        startValue: zStart, endValue: zEnd,
                        bottom: 4, height: 30, showDetail: false,
                        borderColor: CHART.border2, backgroundColor: CHART.sliderBg,
                        fillerColor: CHART.sliderFill,
                        handleStyle: { color: CHART.brand2, borderColor: CHART.brand2 },
                        moveHandleStyle: { color: CHART.brand2 },
                        dataBackground: { lineStyle: { color: CHART.border2 }, areaStyle: { color: CHART.sliderArea } },
                    },
                ],
                grid: [
                    { left: 60, right: 20, top: 20, height: '42%' },
                    { left: 60, right: 20, top: '58%', height: '12%' },
                    { left: 60, right: 20, top: '72%', height: '13%' },
                ],
                xAxis: [
                    { type: 'category', data: dates, axisLine: { lineStyle: { color: CHART.axisLine } },
                      axisLabel: { color: CHART.axisLabel, fontSize: 10 }, gridIndex: 0 },
                    { type: 'category', data: dates, axisLine: { show: false }, axisLabel: { show: false }, gridIndex: 1 },
                    { type: 'category', data: dates, axisLine: { lineStyle: { color: CHART.axisLine } },
                      axisLabel: { color: CHART.axisLabel2, fontSize: 9 }, gridIndex: 2 },
                ],
                yAxis: [
                    { scale: true, axisLine: { show: false }, splitLine: { lineStyle: { color: CHART.grid } },
                      axisLabel: { color: CHART.axisLabel }, gridIndex: 0 },
                    { scale: true, axisLine: { show: false }, splitLine: { show: false },
                      axisLabel: { color: CHART.axisLabel3, fontSize: 10 }, gridIndex: 1 },
                    { scale: true, axisLine: { show: false }, splitLine: { show: false },
                      axisLabel: { color: CHART.axisLabel2, fontSize: 9 }, gridIndex: 2 },
                ],
                // K线 + 成交量 + MACD + 7 条均线
                series: [
                    kSeries,
                    {
                        name: '成交量', type: 'bar', data: vols,
                        itemStyle: { color: (p) => data[p.dataIndex].close >= data[p.dataIndex].open ? CHART.up : CHART.down },
                        xAxisIndex: 1, yAxisIndex: 1,
                    },
                    ...(window.Indicators?.macd ? macdSeries(Indicators.macd(data)) : []),
                    ...maSeries,
                ],
            }, true);
        }

        // ---------- MACD 系列 (DIF/DEA/柱, 红涨绿跌) ----------
        function macdSeries(m) {
            return [
                { name: 'DIF', type: 'line', data: m.dif, xAxisIndex: 2, yAxisIndex: 2,
                  showSymbol: false, smooth: true, lineStyle: { width: 1, color: CHART.orange } },
                { name: 'DEA', type: 'line', data: m.dea, xAxisIndex: 2, yAxisIndex: 2,
                  showSymbol: false, smooth: true, lineStyle: { width: 1, color: CHART.brand } },
                { name: 'MACD', type: 'bar', data: m.hist.map(v => ({
                    value: v, itemStyle: { color: v >= 0 ? CHART.up : CHART.down },
                })), xAxisIndex: 2, yAxisIndex: 2 },
            ];
        }

        // ---------- 绑定 K 线 click ----------
        function bindKlineClick() {
            if (!klineChart) return;
            const zr = klineChart.getZr();
            zr.off('click');
            zr.on('click', (params) => {
                let dataIndex = null;
                try {
                    const r = klineChart.convertFromPixel({ seriesIndex: 0 }, [params.offsetX, params.offsetY]);
                    if (r && r.length) dataIndex = Math.round(r[0]);
                } catch(e) {}
                if (dataIndex == null && params.dataIndex != null) dataIndex = params.dataIndex;
                if (dataIndex != null && dataIndex >= 0) onKlineClick({ dataIndex });
            });
            // 也绑 ECharts 高层 click (双重保险)
            klineChart.off('click');
            klineChart.on('click', onKlineClick);
        }

        // ---------- K 线滚轮分流 (mac 触控板/鼠标统一语义) ----------
        //   ctrl+wheel(捏合)  → 缩放窗口 (改范围)
        //   横向两指滑动       → 平移窗口 (只改位置, 不改范围)
        //   纵向上下滑动       → 不拦截, 交最外层页面滚动
        function klineChain_apply(zStart, zEnd) {
            klineCache.zStart = zStart; klineCache.zEnd = zEnd;
            klineChart.dispatchAction({ type: 'dataZoom', dataZoomIndex: 0, startValue: zStart, endValue: zEnd });
            klineChart.dispatchAction({ type: 'dataZoom', dataZoomIndex: 1, startValue: zStart, endValue: zEnd });
        }
        function bindKlineWheel() {
            if (!klineChart) return;
            const dom = klineChart.getDom();
            if (!dom) return;
            dom.style.touchAction = 'pan-y';           // 纵向触摸交页面
            if (dom._wheelBound) { dom.removeEventListener('wheel', dom._wheelBound); }
            const handler = (e) => {
                const total = klineDataset.value.length;
                if (!total) return;
                let zs = klineCache.zStart, ze = klineCache.zEnd;
                if (zs == null || ze == null) { zs = Math.max(0, total - 120); ze = total - 1; }
                const span = ze - zs + 1;
                if (e.ctrlKey) {
                    // 捏合缩放: 以窗口中心为锚点按 1.1 倍收缩/扩张
                    const factor = e.deltaY > 0 ? 1.1 : 0.9;
                    let nSpan = Math.max(10, Math.round(span * factor));
                    nSpan = Math.min(nSpan, total);
                    const center = (zs + ze) / 2;
                    let ns = Math.round(center - nSpan / 2);
                    let ne = ns + nSpan - 1;
                    if (ns < 0) { ne -= ns; ns = 0; }
                    if (ne > total - 1) { ns -= (ne - (total - 1)); ne = total - 1; }
                    ns = Math.max(0, ns);
                    klineChain_apply(ns, ne);
                    e.preventDefault();
                } else if (Math.abs(e.deltaX) > Math.abs(e.deltaY)) {
                    // 横向滑动: 平移窗口, 宽度不变
                    const shift = Math.round(e.deltaX / 3) || (e.deltaX > 0 ? 1 : -1);
                    let ns = zs + shift, ne = ze + shift;
                    if (ns < 0) { ne -= ns; ns = 0; }
                    if (ne > total - 1) { ns -= (ne - (total - 1)); ne = total - 1; }
                    ns = Math.max(0, ns);
                    klineChain_apply(ns, ne);
                    e.preventDefault();
                }
                // 纵向滑动: 不拦截, 冒泡给最外层页面滚动
            };
            dom._wheelBound = handler;
            dom.addEventListener('wheel', handler, { passive: false });
        }

        // ---------- K 线 click handler ----------
        function onKlineClick({ dataIndex }) {
            if (dataIndex == null || dataIndex < 0 || !klineDataset.value.length) return;
            const bar = klineDataset.value[dataIndex];
            if (!bar) return;
            selectedKlineDate.value = bar.date;
            // 重绘(保留当前缩放窗口)以在 K线 series 上叠加选中高亮, 不破坏其它系列
            drawKline(klineDataset.value, true);
            // 加载对应日期分时
            loadIntraday(bar.date);
        }

        // ---------- 绘制分时 ----------
        function drawIntraday(data) {
            if (!intradayChart || !data?.points?.length) return;
            const times = data.points.map(p => p.time);
            const prices = data.points.map(p => p.price);
            const avgs = data.points.map(p => p.avg);
            const vols = data.points.map(p => p.volume);
            const pc = data.prev_close || data.points[0].price;

            // 纵轴自适应: 按实际数据取范围 (必含昨收) + 8% 余量, 数据越平范围越贴合, 不再拉平失真
            let lo = pc, hi = pc;
            for (let i = 0; i < prices.length; i++) {
                lo = Math.min(lo, prices[i], avgs[i]);
                hi = Math.max(hi, prices[i], avgs[i]);
            }
            const pad = (hi - lo) * 0.08 || pc * 0.004;   // 数据极平时给最小可见余量
            const yMin = +(lo - pad).toFixed(2);
            const yMax = +(hi + pad).toFixed(2);

            intradayChart.setOption({
                backgroundColor: 'transparent', animation: false,
                tooltip: {
                    trigger: 'axis', axisPointer: { type: 'cross' },
                    formatter: (params) => {
                        const i = params[0].dataIndex;
                        const p = data.points[i];
                        const chg = ((p.price - pc) / pc * 100).toFixed(2);
                        return `${p.time}<br/>价格 ${p.price.toFixed(2)} (${chg}%)<br/>均价 ${p.avg.toFixed(2)}<br/>量 ${formatAmount(p.volume)}`;
                    },
                },
                legend: { data: ['价格', '均价'], textStyle: { color: CHART.axisLabel }, top: 0, right: 10 },
                grid: [
                    { left: 60, right: 60, top: 30, height: '58%' },
                    { left: 60, right: 60, top: '74%', height: '18%' },
                ],
                xAxis: [
                    { type: 'category', data: times, boundaryGap: false,
                      axisLine: { lineStyle: { color: CHART.axisLine } }, axisLabel: { color: CHART.axisLabel, fontSize: 10 }, gridIndex: 0 },
                    { type: 'category', data: times, axisLine: { show: false }, axisLabel: { show: false }, gridIndex: 1 },
                ],
                yAxis: [
                    {
                        scale: true, min: yMin, max: yMax,
                        axisLine: { show: false }, splitLine: { lineStyle: { color: CHART.grid } },
                        axisLabel: {
                            color: CHART.axisLabel, fontSize: 10,
                            formatter: (v) => {
                                const pct = ((v - pc) / pc * 100);
                                const sign = pct > 0 ? '+' : '';
                                return sign + pct.toFixed(2) + '%';
                            },
                        },
                        axisPointer: { label: { formatter: (p) => ((p.value - pc) / pc * 100).toFixed(2) + '%' } },
                        gridIndex: 0,
                    },
                    { scale: true, axisLine: { show: false }, splitLine: { show: false },
                      axisLabel: { color: CHART.axisLabel3, fontSize: 10 }, gridIndex: 1 },
                ],
                series: [
                    { name: '价格', type: 'line', data: prices, smooth: true,
                      lineStyle: { color: CHART.brand, width: 1.5 },
                      areaStyle: { color: CHART.brandArea },
                      markLine: {
                          silent: true, symbol: 'none',
                          label: { show: true, color: CHART.dim, fontSize: 10, position: 'insideEndTop' },
                          lineStyle: { color: CHART.dim, type: 'dashed', width: 1 },
                          data: [{ yAxis: pc, name: '昨收' }],
                      },
                      xAxisIndex: 0, yAxisIndex: 0, showSymbol: false },
                    { name: '均价', type: 'line', data: avgs, smooth: true,
                      lineStyle: { color: CHART.orange, width: 1 },
                      xAxisIndex: 0, yAxisIndex: 0, showSymbol: false },
                    { name: '成交量', type: 'bar', data: vols,
                      itemStyle: { color: (p) => prices[p.dataIndex] >= pc ? CHART.up : CHART.down },
                      xAxisIndex: 1, yAxisIndex: 1 },
                ],
            }, true);
        }

        // ---------- 动态分组: 选择态 / 元数据 / 实时求值 ----------
        function dynKey(id) { return DYN_PREFIX + id; }
        const isDynSelection = computed(() =>
            typeof watchGroupId.value === 'string' && watchGroupId.value.startsWith(DYN_PREFIX));
        const currentDynGroupId = computed(() => isDynSelection.value
            ? watchGroupId.value.slice(DYN_PREFIX.length) : null);
        const currentDynGroup = computed(() =>
            dynGroups.value.find(g => dynKey(g.id) === watchGroupId.value) || null);
        const canDeleteGroup = computed(() => {
            if (isDynSelection.value) return !!currentDynGroup.value && !currentDynGroup.value.builtin;
            if (watchGroupId.value === NONE_GROUP || watchGroupId.value == null) return false;
            const g = watchGroups.value.find(x => x.id === watchGroupId.value);
            return !!g && g.name !== '自选';           // 默认分组「自选」不可删
        });

        async function loadDynGroups() {
            if (!isLoggedIn.value) { dynGroups.value = []; return; }
            try {
                dynGroups.value = (await api('/dyn-groups')).groups || [];
            } catch(e) { console.warn('动态分组加载失败:', e.message); dynGroups.value = []; }
        }

        async function loadDynMetrics() {
            if (dynMetrics.value.length) return;
            try { dynMetrics.value = (await api('/dyn-groups/metrics')).metrics || []; }
            catch(e) { console.warn('指标元数据加载失败:', e.message); }
        }

        /** 动态分组个股: 每次按规则对全市场实时求值 (force 时强制重拉快照) */
        async function loadDynStocks(id, force = false) {
            if (id == null) return;
            dynLoading.value = true;
            try {
                const r = await api('/dyn-groups/' + id + '/stocks?limit=200' + (force ? '&refresh=1' : ''));
                stocks.value = r.stocks || [];
            } catch(e) {
                console.warn('动态分组获取失败:', e.message);
                stocks.value = [];
            } finally { dynLoading.value = false; }
        }

        // ---------- 动态分组 规则编辑器 ----------
        function dynMetricMeta(key) { return dynMetrics.value.find(m => m.key === key) || null; }
        function dynIsBool(key) { const m = dynMetricMeta(key); return !!m && m.type === 'bool'; }
        function dynUnit(key) { const m = dynMetricMeta(key); return m ? m.unit : ''; }
        function dynOpsOf(key) { const m = dynMetricMeta(key); return m ? m.ops : ['gt']; }
        function opLabel(o) {
            return { gt: '>', gte: '≥', lt: '<', lte: '≤', eq: '等于', between: '介于' }[o] || o;
        }
        function newCond() {
            const first = dynMetrics.value[0];
            return {
                metric: first ? first.key : 'is_limit_up',
                op: first && first.type === 'bool' ? 'eq' : 'gt',
                value: 0, value1: 0, value2: 0, boolVal: true,
            };
        }
        function onMetricChange(c) {
            const ops = dynOpsOf(c.metric);
            if (!ops.includes(c.op)) c.op = ops.includes('gt') ? 'gt' : ops[0];
        }
        function addDynCond() { dynConds.value.push(newCond()); dynPreviewMsg.value = ''; }
        function removeDynCond(i) { dynConds.value.splice(i, 1); dynPreviewMsg.value = ''; }

        /** 打开规则编辑器: 传 g=已有分组(查看/编辑), 省略=新建 */
        async function openDynEditor(g) {
            await loadDynMetrics();
            if (!dynMetrics.value.length) return;
            dynPreviewMsg.value = '';
            if (g && g.rules) {
                dynEditId.value = g.id;
                dynEditBuiltin.value = !!g.builtin;
                dynName.value = g.name;
                dynConds.value = (g.rules.conditions || []).map(c => {
                    const v = c.value;
                    return {
                        metric: c.metric, op: c.op,
                        value: typeof v === 'number' ? v : 0,
                        value1: Array.isArray(v) ? v[0] : 0,
                        value2: Array.isArray(v) ? v[1] : 0,
                        boolVal: v === true,
                    };
                });
            } else {
                dynEditId.value = null;
                dynEditBuiltin.value = false;
                dynName.value = '';
                dynConds.value = [newCond()];
            }
            if (!dynConds.value.length) dynConds.value = [newCond()];
            dynEditorOpen.value = true;
        }

        /** 条件行 → 规则体 (多条件 AND) */
        function buildRules() {
            const conds = dynConds.value.map(c => {
                let value;
                if (c.op === 'between') value = [Number(c.value1) || 0, Number(c.value2) || 0];
                else if (dynIsBool(c.metric)) value = !!c.boolVal;
                else value = Number(c.value) || 0;
                return { metric: c.metric, op: c.op, value };
            });
            return { logic: 'AND', conditions: conds };
        }

        async function previewDyn() {
            if (!isLoggedIn.value) { dynPreviewMsg.value = '请先登录'; return; }
            try {
                const r = await api('/dyn-groups/preview', { method: 'POST',
                    body: JSON.stringify({ rules: buildRules(), limit: 50 }) });
                dynPreviewMsg.value = `命中 ${r.total} 只`;
            } catch(e) {
                let msg = e.message; try { msg = JSON.parse(e.message).reason || msg; } catch(_) {}
                dynPreviewMsg.value = '预览失败: ' + String(msg).replace(/^"|"$/g, '');
            }
        }

        async function saveDyn() {
            if (!dynName.value.trim()) { dynPreviewMsg.value = '请填写分组名称'; return; }
            if (!isLoggedIn.value) { dynPreviewMsg.value = '请先登录'; return; }
            dynSaving.value = true;
            try {
                const body = { name: dynName.value.trim(), rules: buildRules() };
                if (dynEditId.value && !dynEditBuiltin.value) {
                    await api('/dyn-groups/' + dynEditId.value, { method: 'PUT', body: JSON.stringify(body) });
                } else if (!dynEditId.value) {
                    const g = await api('/dyn-groups', { method: 'POST', body: JSON.stringify(body) });
                    watchGroupId.value = dynKey(g.id);
                }
                dynEditorOpen.value = false;
                await loadDynGroups();
                if (isDynSelection.value) await loadDynStocks(currentDynGroupId.value, true);
            } catch(e) {
                let msg = e.message; try { msg = JSON.parse(e.message).reason || msg; } catch(_) {}
                dynPreviewMsg.value = '保存失败: ' + String(msg).replace(/^"|"$/g, '');
            } finally { dynSaving.value = false; }
        }

        async function deleteDynGroup(id) {
            try { await api('/dyn-groups/' + id, { method: 'DELETE' }); }
            catch(e) { console.warn('删除动态分组失败:', e.message); }
            watchGroupId.value = null;
            await loadDynGroups();
            await refreshWatch();
        }

        // ---------- 设置模块 / 主题 ----------
        function openSettings(tab) {
            if (tab) settingsTab.value = tab;
            settingsOpen.value = true;
        }
        function applyTheme(t) {
            document.documentElement.dataset.theme = t;
            theme.value = t;
            localStorage.setItem('theme', t);
            refreshChartColors();
            window.dispatchEvent(new CustomEvent('themechange', { detail: { theme: t } }));   // 通知子组件(训练/复盘/模拟交易)重绘图表
        }
        /** 切换黑/白主题: 生效后按新配色重绘当前图表 */
        async function setTheme(t) {
            if (theme.value === t) return;
            applyTheme(t);
            if (selectedStock.value && activeTab.value === 'kline') {
                await loadKline();
                if (klineDataset.value.length) await loadIntraday(selectedKlineDate.value);
            }
        }

        // ---------- 初始化 ----------
        onMounted(async () => {
            applyTheme(theme.value);        // 应用已记忆的页面风格 (黑/白)
            // 恢复登录态: 拉取 /auth/me 获取手机号绑定状态 (旧账号需补绑)
            await refreshMe();
            // 加载自选分组 (后端持久化, 默认分组「自选」; 未登录返回空)
            await refreshWatch();
            await loadDynGroups();
            loadAccount();

            // 默认选第一个自选股 (如果有)
            if (stocks.value.length) {
                selectStock(stocks.value[0]);
            }

            // 窗口 resize / 手机横竖屏切换 → resize ECharts
            const resizeCharts = () => { klineChart?.resize(); intradayChart?.resize(); };
            window.addEventListener('resize', resizeCharts);
            window.addEventListener('orientationchange', () => setTimeout(resizeCharts, 300));

            // 行情轮询: 每 15s 刷新自选列表与选中标的的最新价/涨跌幅 (板块视图跳过)
            setInterval(async () => {
                await refreshQuotes();
            }, 15000);

            console.log('✅ App mounted, Console 零错误 = 架构正确');
        });

        onUnmounted(() => {
            window.removeEventListener('resize', () => {});
            disposeCharts();
        });

        // 周期切换后需要重绘 (klinePeriod 变化时 loadKline 已重绘)

        // 供其它组件 (如模拟交易下单面板信息卡) 跳转行情页: 不加入任何自选/分类
        window.__gotoStock = selectStock;

        // ---------- return (暴露给模板) ----------
        return {
            // data
            tabs, activeTab,
            boardOpen, boardKind, boards, boardLoading, boardName, boardStocks, boardConsLoading,
            boardStats, boardSortedAsc, toggleBoardSort,
            boardKw, boardSearching, boardSearchResults, boardList, boardListCount,
            debounceBoardSearch, clearBoardSearch,
            toggleBoard, switchBoardKind, loadBoards, selectBoard, selectBoardStock,
            selectBoardFromSearch,
            boardSel, boardAddGroupId, boardAdding, boardAllSel,
            toggleBoardSel, toggleBoardAll, batchAddBoardStocks,
            searchKw, searchResults,
            watchList, stocks, watchGroups, watchGroupId, newGroupName, watchTitle,
            switchGroup, addGroup, delGroup, removeWatch, reloadWatchView,
            // 动态分组 (按指标实时获取)
            dynGroups, dynKey, isDynSelection, currentDynGroup, canDeleteGroup, dynLoading,
            dynEditorOpen, dynEditId, dynEditBuiltin, dynName, dynConds, dynMetrics,
            dynPreviewMsg, dynSaving,
            openDynEditor, addDynCond, removeDynCond, dynOpsOf, dynIsBool, dynUnit,
            opLabel, onMetricChange, previewDyn, saveDyn,
            // 设置模块 / 页面样式
            settingsOpen, settingsTab, openSettings, theme, setTheme,
            importFile, triggerImport, onImportFile,
            selectedStock, selectedKlineDate,
            klineDataset, intradayData, intradayLoading, intradayAmp,
            klinePeriods, klinePeriod,
            acct, tiers, profit,
            user, authMode, authUser, authPass, authMsg, authLoading, authSubmit, logout,
            authPhone, authEmail, authShowPass, needBindPhone, bindPhoneSubmitting,
            isLoggedIn, submitBindPhone, refreshMe, validPhone,
            // methods
            switchTab, debounceSearch, selectFromSearch, selectStock, refreshWatch,
            loadKline, loadIntraday, onKlineClick, formatAmount, priceClass,
            fmtMoney, fmtVol, setPeriod,
            loadAccount, setTier, resetAccount,
        };
    },
});

// ---------- 注册 Tab 组件 (defineComponent + 独立文件) ----------
if (window.TrainPane) app.component('train-pane', window.TrainPane);
if (window.ReviewPane) app.component('review-pane', window.ReviewPane);
if (window.LivePane) app.component('live-pane', window.LivePane);
if (window.TradeOrderBox) app.component('trade-order-box', window.TradeOrderBox);

app.mount('#app');

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
    // 需求101602: 统一走预加载/缓存层 (只读GET缓存 + 并发去重), 见 preload.js
    return window.__apiFetch(path, opts);
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
        // 需求101801-1: tip = 入口「?」悬浮提示文案
        const tabs = [
            { id: 'train', label: '训练', icon: '🎯', cls: 'train',
              tip: '零风险盘感训练：逐根 K 线推进，每根做【买入/卖出/观望】决策，结束自动结算并于复盘揭晓标的与盈亏。' },
            { id: 'live', label: '模拟交易', icon: '💹', cls: 'live',
              tip: '模拟交易：用真实行情做零资金风险的下单演练，含持仓 / 委托 / 成交流水与盈亏统计。' },
            { id: 'kline', label: '自选', icon: '⭐', cls: 'kline',
              tip: '自选：分组管理关注的个股 / 板块 / 指数，单击选中、双击看详情；支持动态分组按指标实时筛选。' },
            // 需求101804-2: 「图表」模块文案 → 「行情」
            { id: 'chart', label: '行情', icon: '📈', cls: 'kline',
              tip: '行情：K 线 + 分时 + 指标叠加，可切换周期，左侧抽屉切换分组个股。' },
            // 需求101804-3: 「看盘」模块文案 → 「盘面」
            { id: 'watch', label: '盘面', icon: '📡', cls: '',
              tip: '盘面：全市场快照与板块 / 指数全景，快速发现当日强弱方向。' },
            { id: 'review', label: '复盘', icon: '📊', cls: '',
              tip: '复盘：训练与模拟交易的成绩统计、成长曲线与历史记录回顾。' },
        ];
        // 需求101804-0: 系统界面默认定位到训练模块
        const activeTab = ref('train');
        /** 需求101803-0: 模块页面右上角「?」的说明文案 (取自 tab.tip) */
        function tabTip(id) {
            const t = tabs.find(x => x.id === id);
            return t ? t.tip : '';
        }

        const searchKw = ref('');
        const searchResults = ref({ stocks: [], boards: [] });   // 聚合搜索结果 (个股 + 板块)
        const searchTimer = ref(null);

        const watchList = ref([]);       // 当前分组自选 code 列表 (后端持久化)
        const stocks = ref([]);          // 当前分组自选股带行情数据
        const watchGroups = ref([]);     // [{id, name, items: []}]
        const watchGroupId = ref(null);
        const newGroupName = ref('');
        // 需求8: 「新建分组」统一入口 (下拉选类型) / 自选分组命名输入
        const newGroupOpen = ref(false);
        const newStaticOpen = ref(false);
        const watchOverview = ref({});   // 需求9: {group_id: 分组涨跌幅}
        const importFile = ref(null);    // 隐藏的文件选择框 (导入自选)
        let loadingWatch = false;    // 并发保护

        // ---------- 需求101404-0: 自选股可配置表头 (图1「添加表头」) ----------
        // 字段目录: 4 大分类, 与同花顺「添加表头」一致
        const WL_FIELD_CATS = [
            { key: 'basic', label: '基础行情', fields: [
                ['change_pct', '涨幅%'], ['price', '现价'], ['change', '涨跌'], ['speed', '涨速%'],
                ['vol_ratio', '量比'], ['turnover', '换手%'], ['cur_vol', '现量'], ['volume', '总量'],
                ['amount', '总金额'], ['avg_amt', '均笔额'], ['buy1', '买一'], ['sell1', '卖一'],
                ['prev_close', '昨收'], ['open', '开盘'], ['high', '最高'], ['low', '最低'],
                ['amplitude', '振幅%'],
            ] },
            { key: 'finance', label: '财务数据', fields: [
                ['eps', '每股收益'], ['bps', '每股净资产'], ['reserve', '每股公积金'],
                ['undist', '每股未分配利润'], ['roe', '净资产收益'], ['net_profit', '净利润'],
                ['pb', '市净率'], ['profit_growth', '净利润增长率'], ['debt_ratio', '资产负债率'],
                ['pe_dyn', '市盈(动)'], ['pe_static', '市盈(静)'], ['pe_ttm', '市盈TTM'],
            ] },
            { key: 'fund', label: '资金数据', fields: [
                ['main_net_vol', '主力净量'], ['retail_num', '散户数量'], ['main_net_in', '主力净流入'],
                ['main_in', '主力流入'], ['main_out', '主力流出'], ['dd_net_5', '5日大单净量'],
                ['dd_net_10', '10日大单净量'], ['dd_in', '大单流入'], ['dd_out', '大单流出'],
                ['dd_net', '大单净额'], ['dd_net_pct', '大单净额占比%'], ['dd_total', '大单总额'],
            ] },
            { key: 'special', label: '特色数据', fields: [
                ['category', '分类标示'], ['buy_signal', '买入信号'], ['sell_signal', '卖出信号'],
                ['tech_shape', '技术形态'], ['chairman', '董事长姓名'], ['chairman_school', '董事长毕业学校'],
                ['chairman_value', '董事长持股市值'], ['list_date', '上市日期'], ['list_days', '上市天数'],
                ['main_product', '主营产品'], ['competitor', '竞争对手'], ['search_more', '搜索更多...'],
            ] },
        ];
        const WL_FIELD_ALL = WL_FIELD_CATS.reduce((a, c) => a.concat(c.fields), []);
        const WL_FIELD_LABEL = {};
        WL_FIELD_ALL.forEach(([k, t]) => { WL_FIELD_LABEL[k] = t; });
        // 非数值字段 (左对齐文本列)
        const WL_TEXT_FIELDS = new Set(['category', 'buy_signal', 'sell_signal', 'tech_shape',
            'chairman', 'chairman_school', 'chairman_value', 'list_date', 'main_product',
            'competitor', 'search_more']);
        // 图1 默认选中 (蓝色描边): 基础行情全部 + 市盈TTM + 主力净量/散户数量/主力净流入 + 分类标示/买入信号/卖出信号
        const WL_FIELD_DEFAULT = [
            'change_pct', 'price', 'change', 'speed', 'vol_ratio', 'turnover', 'cur_vol', 'volume',
            'amount', 'avg_amt', 'buy1', 'sell1', 'prev_close', 'open', 'high', 'low', 'amplitude',
            'pe_ttm', 'main_net_vol', 'retail_num', 'main_net_in', 'category', 'buy_signal', 'sell_signal',
        ];
        const WL_LS_KEY = 'wl_header_fields';
        function loadWlFields() {
            try {
                const raw = JSON.parse(localStorage.getItem(WL_LS_KEY) || 'null');
                if (Array.isArray(raw) && raw.length) {
                    const valid = raw.filter(k => WL_FIELD_LABEL[k]);
                    if (valid.length) return valid;
                }
            } catch (e) { /* ignore */ }
            return WL_FIELD_DEFAULT.slice();
        }
        const wlFields = ref(loadWlFields());          // 当前表头字段 (按目录顺序展示)
        const wlHeaderOpen = ref(false);
        const wlHeaderKw = ref('');                    // 弹窗搜索词
        const wlHeaderDraft = ref([]);                 // 弹窗内草稿选择
        const wlCatOpen = ref({ basic: true, finance: true, fund: true, special: true });
        const wlActiveFields = computed(() =>
            WL_FIELD_ALL.map(([k]) => k).filter(k => wlFields.value.includes(k)));
        // 需求101505-0: 「图表」模块左侧个股列表只展示 名称/代码/涨跌幅
        // (名称/代码为固定列, 此处的可变列固定为 change_pct; 「自选」模块仍按「添加表头」配置)
        const wlCols = computed(() =>
            activeTab.value === 'chart' ? ['change_pct'] : wlActiveFields.value);
        const wlHeaderCats = computed(() => {
            const kw = wlHeaderKw.value.trim().toLowerCase();
            return WL_FIELD_CATS.map(c => {
                const fields = c.fields.filter(([k, t]) =>
                    !kw || t.toLowerCase().includes(kw) || k.toLowerCase().includes(kw));
                return { key: c.key, label: c.label, fields,
                         open: wlCatOpen.value[c.key] || !!kw, total: c.fields.length };
            }).filter(c => !kw || c.fields.length);
        });

        // ---------- 需求101404-1: 自选股双击 → 个股详情 (图2 三栏形态) ----------
        const sdOpen = ref(false);
        const sdStock = ref(null);
        const sdPeriod = ref('intraday');              // intraday | day | week | month | year
        const sdLoading = ref(false);
        const sdMsg = ref('');
        const sdKline = ref([]);
        const sdPeriods = [
            { key: 'intraday', label: '分时' }, { key: 'day', label: '日K' }, { key: 'week', label: '周K' },
            { key: 'month', label: '月K' }, { key: 'year', label: '年K' },
        ];
        const sdBottomTabs = ['超级盘口', '量比', 'MACD', 'KDJ', 'RSI', '大单净量', '高抛低吸(K)', '成交对比'];
        const sdBottomTab = ref('超级盘口');
        const sdNewsTabs = ['新闻资讯', '关联股票', '持股基金', '关联期商', '快速交易', '股市便签'];
        const sdNewsTab = ref('新闻资讯');

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
        const dynTab = ref('最近使用');    // 当前分类 Tab
        const dynRecent = ref([]);       // 最近使用过的指标 key (最近优先, 按账号持久化)
        const dynPreviewTotal = ref(null);   // 上次「查看结果」命中数 (null = 尚未查看)

        // ---------- 设置模块 / 主题 ----------
        const settingsOpen = ref(false);
        const settingsTab = ref('account');    // account | style | manual | about
        const theme = ref(localStorage.getItem('theme') || 'dark');

        // ---------- 需求101801-0: 首次登录引导 ----------
        const onboardOpen = ref(false);
        const onboardStep = ref(0);
        // 需求101803-2: 引导按模块逐一说明, 不做合并
        const onboardSteps = [
            { icon: '🎯', title: '训练 · 零风险盘感训练', tab: 'train',
              desc: '从「训练」进入：选品类(股票双盲/长周期/妖股等) → 设根数与标的 → 点「准备开始训练」。逐根 K 线推进，每根做【买入 / 卖出 / 观望】决策，到末尾自动结算；双盲模式训练中隐藏标的与日期，结束后揭晓，成绩可在「复盘」查看。' },
            { icon: '💹', title: '模拟交易 · 真实行情下单演练', tab: 'live',
              desc: '从「模拟交易」进入：使用真实行情做零资金风险的买卖委托，先选资金体系，再搜索标的、填价格与数量下单；账户资金 / 持仓 / 委托 / 成交流水实时更新，盈亏与风控指标一目了然。' },
            { icon: '⭐', title: '自选 · 分组管理关注的标的', tab: 'kline',
              desc: '在「自选」中按分组管理个股 / 板块 / 指数，单击选中、双击看详情；可新建自选分组，或用动态分组按指标实时筛选；也支持从 txt/csv/tsv/json 文件导入自选。' },
            { icon: '📈', title: '行情 · K 线与分时', tab: 'chart',
              desc: '从「行情」进入：查看选中标的的 K 线 + 分时 + 指标(MACD/均线)，可切换周期与单根查看；左侧抽屉可收起 / 拉出分组个股，板块与指数有专属视图，右侧常驻模拟交易下单面板。' },
            { icon: '📡', title: '盘面 · 全市场快照', tab: 'watch',
              desc: '「盘面」聚合同花顺 / 开盘啦结构：国内指数、日韩美股、市场宽度、异动板块与昨日涨停股一屏看清当日强弱，点击可跳转到「行情」查看。' },
            { icon: '📊', title: '复盘 · 回顾每一步成绩', tab: 'review',
              desc: '「复盘」按「训练 / 模拟交易」双子页汇总成绩统计、成长曲线、胜率与盈亏分布，支持按品类 / 战法 / 时间维度筛选历史明细。' },
        ];
        // 需求101801-2: 操作手册 (文档集)
        const manualDocId = ref('quickstart');
        const manualDocs = [
            { id: 'quickstart', icon: '🚀', title: '快速上手', desc: '3 分钟走完核心流程：注册登录 → 训练 → 模拟交易。',
              sections: [
                  { h: '① 账号注册与登录', items: [
                      '点击右上角 ⚙「设置 → 账号密码」，切到「注册」页签。',
                      '账号格式：4-20 位，以字母开头，仅含字母 / 数字 / 下划线(如 trainee01)。',
                      '密码复杂度：至少 8 位，且需包含大写字母、小写字母、数字、特殊字符中的至少三类(如 Abcd1234!)。',
                      '手机号必填且全局唯一，邮箱选填；手机号用于账号识别与查找。',
                  ]},
                  { h: '② 训练核心流程', items: [
                      '进入「训练」模块 → 选择品类 → 设置单局 K 线根数 / 初始资金 / 杠杆。',
                      '可选填「指定标的」与「起始日期」：留空 = 随机抽取并双盲(隐藏名称与日期)。',
                      '点击「准备开始训练」，进入后逐根推进，每根做出 买入 / 卖出 / 观望 决策。',
                      '推进到末尾自动结算，双盲标的在结束时揭晓，可在「复盘」回看。',
                  ]},
                  { h: '③ 模拟交易核心流程', items: [
                      '进入「模拟交易」模块 → 搜索并选择标的 → 输入价格与数量下单。',
                      '在东财 / 委托 / 持仓 / 成交流水中跟踪状态，账户资金与盈亏实时更新。',
                      '模拟交易使用真实行情，但资金为虚拟，零风险练手。',
                  ]},
              ]},
            { id: 'train', icon: '🎯', title: '训练模块', desc: '逐根 K 线推进的盘感训练，重点在「决策」而非预测。',
              sections: [
                  { h: '品类与参数', items: [
                      '品类：股票双盲(≤250 根) / 长周期训练(≤1250 根)；不同品类杠杆与涨跌停桶不同。',
                      '单局 K 线根数：决定一局推进的长度，越长越考验持仓与节奏。',
                      '初始资金 / 杠杆：影响盈亏幅度与爆仓风险。',
                  ]},
                  { h: '决策操作', items: [
                      '买入 / 卖出：按当前根价格成交，仓位与资金实时结算。',
                      '观望：跳过分歧、等待更优位置；也可用「条件单」预设触发价。',
                      '周期可切换(日/周/月等)，切换后按新周期粒度继续推进。',
                  ]},
                  { h: '结算与复盘', items: [
                      '推进到末尾自动结算，生成盈亏、收益率、最大回撤、胜率等指标。',
                      '双盲模式结束时揭示标的名称、区间与日期。',
                      '所有记录可在「复盘」模块按品类 / 战法 / 时间筛选回顾。',
                  ]},
              ]},
            { id: 'live', icon: '💹', title: '模拟交易', desc: '真实行情下的零风险下单演练。',
              sections: [
                  { h: '下单', items: [
                      '搜索选择标的，填写价格与数量后提交委托；支持限价 / 市价。',
                      '委托进入「委托」列表，成交后写入「成交流水」。',
                  ]},
                  { h: '账户与持仓', items: [
                      '账户资金分档(基础 / 进阶 / 专业 / 大户)，可在账户页切换。',
                      '持仓浮动盈亏、可用资金随行情实时刷新，含佣金 / 印花税 / 过户费。',
                  ]},
              ]},
            { id: 'watch', icon: '📡', title: '自选 · 行情 · 盘面 · 复盘', desc: '行情浏览与账号维度的记录回顾。',
              sections: [
                  { h: '自选与行情', items: [
                      '自选支持分组，单击选中、双击看个股详情；分组标签上的 ✕ 可删除该分组（默认分组「自选」除外）；动态分组按指标实时筛选。',
                      '行情展示 K 线 + 分时 + 指标，可切换周期、叠加指标。',
                  ]},
                  { h: '盘面', items: [
                      '全市场快照与板块 / 指数全景，快速识别当日强弱。',
                      '点击指数 / 板块 / 个股可跳转到「行情」查看。',
                  ]},
                  { h: '复盘', items: [
                      '按品类 / 战法 / 时间维度统计训练与模拟交易成绩。',
                      '查看成长曲线、胜率、盈亏分布与历史明细。',
                  ]},
              ]},
            { id: 'faq', icon: '❓', title: '账号与常见问题', desc: '账号规则、帮助入口与常见疑问。',
              sections: [
                  { h: '账号与安全', items: [
                      '账号格式：4-20 位，字母开头，仅字母 / 数字 / 下划线。',
                      '密码：≥8 位，含大小写 / 数字 / 特殊字符至少三类；登录会话 7 天有效。',
                      '手机号全局唯一；老账号未绑手机号时需先补绑方可使用。',
                  ]},
                  { h: '帮助入口', items: [
                      '未登录账号首次登录成功后会弹出「首次使用引导」，介绍核心流程。',
                      '关键功能入口旁有「?」问号，鼠标悬停即可查看该功能的简短说明。',
                      '本操作手册位于「设置 → 操作手册」，可按文档切换；也可随时点「重看首次引导」。',
                  ]},
              ]},
        ];
        const manualDoc = computed(() =>
            manualDocs.find(d => d.id === manualDocId.value) || manualDocs[0]);

        /** 需求101803-1: 设备标识(每台设备/浏览器一个, 持久保存在 localStorage) */
        function deviceId() {
            let id = null;
            try { id = localStorage.getItem('device_id'); } catch (e) {}
            if (!id) {
                id = 'd' + Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
                try { localStorage.setItem('device_id', id); } catch (e) {}
            }
            return id;
        }
        /** 需求101803-1: 引导展示标记按 device-id 隔离 —— 同一设备只展示一次 */
        function onboardKey() {
            return 'onboard_done_' + deviceId();
        }
        function maybeOnboard(force) {
            if (!isLoggedIn.value) return;
            if (!force && localStorage.getItem(onboardKey())) return;
            onboardStep.value = 0;
            onboardOpen.value = true;
        }
        function closeOnboard(markDone) {
            onboardOpen.value = false;
            if (markDone !== false) { try { localStorage.setItem(onboardKey(), '1'); } catch (e) {} }
        }
        function onboardGo(i) {
            onboardStep.value = Math.max(0, Math.min(i, onboardSteps.length - 1));
        }
        /** 「去体验」: 关闭并跳到对应模块 */
        function onboardGoTab(tab) {
            closeOnboard(true);
            settingsOpen.value = false;
            if (tab) switchTab(tab);
        }

        const selectedStock = ref(null);
        const selectedKlineDate = ref(null);
        const indexList = ref([]);       // 需求5: 大盘指数清单 (看盘点击跳转用)

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
        // 需求101801: 账号格式约定 (4-20位, 字母开头, 仅字母/数字/下划线)
        const USERNAME_RE = /^[A-Za-z][A-Za-z0-9_]{3,19}$/;
        function validPhone(v) { return PHONE_RE.test(String(v || '').trim()); }
        function validUsername(v) { return USERNAME_RE.test(String(v || '').trim()); }
        /** 密码复杂度: 8-64位, 含大写/小写/数字/特殊字符至少三类, 无空格; 返回错误文案(空=合规) */
        function pwStrengthError(pw) {
            const s = String(pw || '');
            if (s.length < 8) return '密码至少 8 位';
            if (s.length > 64) return '密码最多 64 位';
            if (/\s/.test(s)) return '密码不能包含空格';
            let n = 0;
            if (/[a-z]/.test(s)) n++;
            if (/[A-Z]/.test(s)) n++;
            if (/\d/.test(s)) n++;
            if (/[^A-Za-z0-9]/.test(s)) n++;
            if (n < 3) return '密码需包含大写字母、小写字母、数字、特殊字符中的至少三类';
            return '';
        }
        /** 登录态变化广播: 训练/复盘等子组件按当前账号刷新界面 (与 themechange 同一机制) */
        function notifyAuth() { window.dispatchEvent(new CustomEvent('authchange')); }

        (function restoreSession() {
            const raw = localStorage.getItem('auth_user');
            if (raw) { try { user.value = JSON.parse(raw); } catch (e) {} }
        })();

        /** 拉取 /auth/me: 刷新手机号绑定状态 (登录态恢复后判断是否需要补绑) */
        async function refreshMe() {
            if (!localStorage.getItem('auth_token')) {
                user.value = null; needBindPhone.value = false; notifyAuth(); return;
            }
            try {
                const me = await api('/auth/me');
                if (!me.logged_in) { user.value = null; needBindPhone.value = false; notifyAuth(); return; }
                user.value = { user_id: me.user_id, username: me.username,
                               phone: me.phone || '', email: me.email || '' };
                localStorage.setItem('auth_user', JSON.stringify(user.value));
                needBindPhone.value = !me.phone_bound;
            } catch (e) {
                user.value = null; needBindPhone.value = false;   // 401 已在 api() 清理 token
            }
            notifyAuth();
        }

        async function authSubmit() {
            authMsg.value = '';
            if (!authUser.value.trim() || !authPass.value) {
                authMsg.value = '请输入用户名和密码'; return;
            }
            if (authMode.value === 'register') {
                if (!validUsername(authUser.value)) {
                    authMsg.value = '账号需 4-20 位, 以字母开头, 仅含字母/数字/下划线'; return;
                }
                const pwErr = pwStrengthError(authPass.value);
                if (pwErr) { authMsg.value = pwErr; return; }
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
                maybeOnboard();             // 需求101801-0: 首次登录成功 → 弹出核心流程引导
                settingsOpen.value = false; // 登录完成即收起设置面板, 让引导完整可见
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
                notifyAuth();
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
            notifyAuth();
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
        let sdChart = null;              // 需求101404-1: 个股详情弹层主图实例
        // K 线可视窗口缓存 (索引口径), 供 wheel 分流与重绘保持窗口一致
        const klineCache = { zStart: null, zEnd: null };

        function disposeCharts() {
            try { klineChart?.dispose(); } catch(e) {}
            try { intradayChart?.dispose(); } catch(e) {}
            try { sdChart?.dispose(); } catch(e) {}
            klineChart = null;
            intradayChart = null;
            sdChart = null;
        }

        // ---------- Tab 切换 ----------
        function switchTab(tabId) {
            activeTab.value = tabId;
            // v-show 不需要 DOM 操作, 只需 resize ECharts
            nextTick(() => {
                if (tabId === 'chart') {
                    klineChart?.resize(); intradayChart?.resize();
                    // 需求101601-0: 进入图表模块刷新分时 —— 未点选历史K线时取「当日最新」
                    if (selectedStock.value) linkIntradayToSelected();
                }
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
        // 分时联动到指定索引 (缺省用当前选中日期或「当日最新」)
        function linkIntradayToSelected(idx = -1) {
            const data = klineDataset.value;
            if (!data?.length) return;
            // 需求101601-0: 未显式点选某根K线时展示「当日最新」分时 ——
            // 不传 date 交由后端按开市规则取 今日/上一交易日; 不可回落到 data[last].date,
            // 因为周/月/年线聚合根的日期是「周期首日」(如 10-01), 透传会落到历史某天
            if (idx < 0 && !selectedKlineDate.value) { loadIntraday(null); return; }
            const bar = idx >= 0 ? data[idx] : data.find(d => d.date === selectedKlineDate.value) || data[data.length - 1];
            if (bar) loadIntraday(bar.date);
        }

        // ---------- 需求101503-0: 搜索改为「本地映射数据」匹配 (代码 / 文案名 / 拼音首字母·全拼 全兼容) ----------
        // 本地映射数据 = 股票注册表(含拼音列) + 板块(含拼音), 首次拉取后写入 localStorage 复用
        const LOCAL_IDX_KEY = 'local_map_idx_v1';
        const LOCAL_IDX_TTL = 12 * 3600 * 1000;      // 12h 内直接复用本地映射, 过期再拉一次
        let localIdx = null;
        let localIdxTask = null;

        async function ensureLocalIndex() {
            if (localIdx) return localIdx;
            try {   // 命中浏览器本地存储的映射数据 → 直接复用
                const raw = JSON.parse(localStorage.getItem(LOCAL_IDX_KEY) || 'null');
                if (raw && raw.ts && (Date.now() - raw.ts) < LOCAL_IDX_TTL
                    && (raw.stocks || []).length) { localIdx = raw; return localIdx; }
            } catch (e) { /* ignore */ }
            if (localIdxTask) return localIdxTask;
            localIdxTask = (async () => {
                try {
                    const [stocks, ind, con] = await Promise.all([
                        api('/stocks'),
                        api('/boards?kind=industry').catch(() => ({ boards: [] })),
                        api('/boards?kind=concept').catch(() => ({ boards: [] })),
                    ]);
                    const boards = []
                        .concat((ind.boards || []).map(b => ({ ...b, kind: 'industry' })))
                        .concat((con.boards || []).map(b => ({ ...b, kind: 'concept' })));
                    localIdx = { ts: Date.now(), stocks: stocks || [], boards };
                    try { localStorage.setItem(LOCAL_IDX_KEY, JSON.stringify(localIdx)); } catch (e) { /* ignore */ }
                } catch (e) {
                    console.error('本地映射数据加载失败:', e.message);
                    localIdx = { ts: Date.now(), stocks: [], boards: [] };
                } finally { localIdxTask = null; }
                return localIdx;
            })();
            return localIdxTask;
        }

        /** 本地匹配: 精确 > 代码前缀 > 拼音前缀 > 名称前缀 > 包含 (fields 顺序即优先顺序) */
        function localMatch(list, kw, limit, fields) {
            const q = kw.toLowerCase();
            const hit = [];
            for (const it of list) {
                let rank = -1;
                for (let i = 0; i < fields.length; i++) {
                    const v = String(it[fields[i]] || '').toLowerCase();
                    if (!v) continue;
                    let r = -1;
                    if (v === q) r = i;                  // 精确命中
                    else if (v.startsWith(q)) r = 10 + i; // 前缀命中
                    else if (v.includes(q)) r = 20 + i;   // 包含命中
                    if (r >= 0 && (rank < 0 || r < rank)) rank = r;
                }
                if (rank >= 0) hit.push({ rank, it });
            }
            hit.sort((a, b) => a.rank - b.rank
                || String(a.it.code || a.it.name || '').localeCompare(String(b.it.code || b.it.name || '')));
            return hit.slice(0, limit).map(x => x.it);
        }

        function debounceSearch() {
            if (searchTimer.value) clearTimeout(searchTimer.value);
            const kw = searchKw.value.trim();
            if (!kw) { searchResults.value = { stocks: [], boards: [] }; return; }
            searchTimer.value = setTimeout(async () => {
                const idx = await ensureLocalIndex();
                searchResults.value = {
                    stocks: localMatch(idx.stocks, kw, 12, ['code', 'pinyin', 'name']),
                    boards: localMatch(idx.boards, kw, 8, ['name', 'pinyin', 'label']),
                };
            }, 180);
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
            boardActive.value = false;
            // 刷新自选股行情 → 再 selectStock
            await refreshWatch();
            const fresh = stocks.value.find(x => x.code === s.code);
            selectStock(fresh || { ...s, price: 0 });
        }

        // ---------- 需求101503-2: 从搜索结果选中板块 → 直接展示板块 K线 + 分时 (左侧按涨跌幅展示成分股) ----------
        async function selectBoardFromSearch(b) {
            searchKw.value = '';
            searchResults.value = { stocks: [], boards: [] };
            await selectBoard(b, b.kind);
        }

        // ---------- 板块查询 (行业/概念) — 需求101503-1: 图表模块已删除「板块」按钮/面板, 板块统一由搜索进入 ----------
        const boardOpen = ref(false);
        const boardKind = ref('industry');
        const boardActive = ref(false);   // 需求101503-2: 左侧列表当前展示的是「板块成分股」(按涨跌幅排序)
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
        /** 需求101503-2: 列表按涨跌幅从大到小 (缺值排最后) */
        function sortByPctDesc(list) {
            return (list || []).slice().sort((a, b) => {
                const x = typeof a.change_pct === 'number' ? a.change_pct : -Infinity;
                const y = typeof b.change_pct === 'number' ? b.change_pct : -Infinity;
                return y - x;
            });
        }
        /** 选择板块: 加载成分股 + 板块K线/分时 (对标同花顺板块行情) */
        async function selectBoard(b, kind) {
            const k = kind || boardKind.value;
            boardKind.value = k;
            boardName.value = b.name;
            boardSel.value = [];        // 切换板块清空多选, 避免跨板块残留
            boardConsLoading.value = true;
            try {
                boardStocks.value = (await api('/boards/' + encodeURIComponent(b.label) + '/stocks?kind=' + k)).stocks || [];
            } catch(e) { console.error('成分股加载失败:', e.message); boardStocks.value = []; }
            finally { boardConsLoading.value = false; }
            // 需求101503-2: 左侧列表 = 该板块成分股, 按涨跌幅从大到小
            boardActive.value = true;
            stocks.value = sortByPctDesc(boardStocks.value.map(s => ({
                code: s.code, name: s.name, price: s.price,
                change_pct: s.pct, pct: s.pct, market: marketOf(s.code),
            })));
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

        /** 需求101506-2: 自选项(kind/meta) → 列表行 (个股/板块/指数统一形态) */
        function wlItemToRow(it) {
            let meta = {};
            try { meta = JSON.parse(it.meta || '{}') || {}; } catch(e) { meta = {}; }
            const kind = it.kind || 'stock';
            const base = { wlId: it.id, wlKind: kind, name: it.name,
                           price: it.price ?? 0, change_pct: it.change_pct ?? null,
                           prev_close: it.prev_close ?? null };
            if (kind === 'board') {
                return { ...base, code: it.code, isBoard: true,
                         boardKind: meta.boardKind || 'industry', label: meta.label || it.name };
            }
            if (kind === 'index') {
                return { ...base, code: meta.code || it.code, symbol: meta.symbol || null,
                         isBoard: true, isIndex: true, boardKind: 'index' };
            }
            return { ...base, code: it.code, market: marketOf(it.code) };
        }
        /** 需求101506-2: 点击自选行 → 按类型进入图表 (个股/板块/指数) */
        function openWlRow(s) {
            if (!s) return;
            if (s.isIndex) { selectIndex({ code: s.code, name: s.name, price: s.price, pct: s.change_pct }); return; }
            if (s.isBoard) { selectBoard({ name: s.name, label: s.label }, s.boardKind); return; }
            selectStock(s);
        }

        async function refreshWatch() {
            if (loadingWatch) return;
            loadingWatch = true;
            loadWatchOverview();          // 需求9: 同步刷新各分组涨跌幅标签
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
                // 需求101605: 预取「其余分组」报价 → 切换分组时直接命中前端缓存 (8s TTL), 不再等待后端
                if (window.__apiFetch) {
                    watchGroups.value.forEach(function (x) {
                        if (x.id === watchGroupId.value) return;
                        try { window.__apiFetch('/watchlist/quotes?group_id=' + x.id, { method: 'GET' }).catch(function () {}); } catch (e) {}
                    });
                }
                if (!watchList.value.length) { stocks.value = []; return; }
                // 需求101506-1: 一次性批量取回本组报价 (替代逐股串行 /stocks/{code})
                let items;
                try {
                    const gid = currentGroupId();
                    const q = await api('/watchlist/quotes' + (gid != null ? '?group_id=' + gid : ''));
                    items = q.items || [];
                } catch(e) {
                    console.warn('自选批量报价失败:', e.message);
                    items = watchList.value.map(x => ({ ...x, price: 0 }));
                }
                // 需求101503-2: 左侧列表按涨跌幅从大到小
                const sorted = sortByPctDesc(items.map(wlItemToRow));
                // 需求101506-2: 板块成分股视图下不覆盖左侧列表 (仅更新 watchList, 供 inWatch 判定)
                if (!boardActive.value) stocks.value = sorted;
                // 如果已选中的股票有了新行情, 更新
                if (selectedStock.value) {
                    const cur = selectedStock.value;
                    const fresh = sorted.find(x => x.code === cur.code && !!x.isBoard === !!cur.isBoard);
                    if (fresh) selectedStock.value = { ...cur, price: fresh.price, change_pct: fresh.change_pct };
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
            boardActive.value = false;   // 需求101503-2: 切回分组视图, 退出「板块成分股」列表
            if (isDynSelection.value) await loadDynStocks(currentDynGroupId.value, force);
            else if (watchGroupId.value === NONE_GROUP) await loadUngrouped();
            else await refreshWatch();
        }

        /** 加载「未加入任何分组」的股票 (注册表中有、且不在用户任一自选分组) */
        async function loadUngrouped() {
            try {
                const r = await api('/watchlist/ungrouped?limit=300');
                stocks.value = sortByPctDesc(r.items || []);   // 需求101503-2: 按涨跌幅从大到小
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
            if (isDynSelection.value) return;   // 需求101502-0: 动态分组结果已缓存, 轮询不重算, 仅「刷新」按钮重新获取
            if (boardActive.value) return;      // 需求101503-2: 板块成分股视图不轮询
            if (!stocks.value.length) return;
            if (watchGroupId.value === NONE_GROUP) return;   // 「无」视图为浏览列表, 不轮询
            // 需求101506-1: 批量报价一次取回, 替代逐股轮询
            let rows;
            try {
                const gid = currentGroupId();
                const q = await api('/watchlist/quotes' + (gid != null ? '?group_id=' + gid : ''));
                rows = (q.items || []).map(wlItemToRow);
            } catch(e) { return; }
            const keyOf = r => (r.isIndex ? 'I:' : r.isBoard ? 'B:' : 'S:') + r.code;
            const byKey = {};
            rows.forEach(r => { byKey[keyOf(r)] = r; });
            // 需求101607: 就地更新字段 (仅触发发生变化行的重渲染), 不再整体替换数组/新建对象
            stocks.value.forEach(s => {
                const f = byKey[keyOf(s)];
                if (!f) return;
                if (s.price !== f.price || s.change_pct !== f.change_pct || s.prev_close !== f.prev_close) {
                    s.price = f.price; s.change_pct = f.change_pct; s.prev_close = f.prev_close;
                }
            });
            if (selectedStock.value) {
                const cur = selectedStock.value;
                const f = byKey[keyOf(cur)];
                if (f) { cur.price = f.price; cur.change_pct = f.change_pct; }
            }
        }

        /** 需求9: 各分组涨跌幅 (成分股涨跌幅均值) 供顶部标签栏展示 */
        async function loadWatchOverview() {
            if (!isLoggedIn.value) { watchOverview.value = {}; return; }
            try {
                const r = await api('/watchlist/overview');
                const map = {};
                (r.groups || []).forEach(function (g) { map[g.id] = g.pct; });
                watchOverview.value = map;
            } catch(e) { watchOverview.value = {}; }
        }
        /** 分组标签上的涨跌幅: 各「自选分组」恒取后端概览(成分股均值), 不受当前选中类型影响 */
        function grpPct(g) {
            return { pct: watchOverview.value[g.id] ?? null };
        }
        /** 需求8: 统一「新建分组」入口 → 选类型 */
        function toggleNewGroup() {
            newGroupOpen.value = !newGroupOpen.value;
            if (newGroupOpen.value) newStaticOpen.value = false;
        }
        function startNewStatic() {
            newGroupOpen.value = false;
            newStaticOpen.value = true;
            newGroupName.value = '';
        }
        function startNewDyn() {
            newGroupOpen.value = false;
            newStaticOpen.value = false;
            openDynEditor();
        }

        async function addGroup() {
            const name = newGroupName.value.trim();
            if (!name) return;
            try {
                const g = await api('/watchlist/group', { method: 'POST', body: JSON.stringify({ name }) });
                watchGroupId.value = g.id;
            } catch(e) { console.warn('新建分组失败:', e.message); }
            newGroupName.value = '';
            newStaticOpen.value = false;
            await refreshWatch();
        }

        // ---------- 需求101804-1: 自选分组标签上的 ✕ 删除入口 ----------
        /** 静态分组: 默认分组「自选」不可删 */
        function canDelGroupById(id) {
            const g = watchGroups.value.find(x => x.id === id);
            return !!g && g.name !== '自选';
        }
        async function delGroupById(id) {
            if (!canDelGroupById(id)) return;
            try {
                await api('/watchlist/group/' + id, { method: 'DELETE' });
            } catch(e) { console.warn('删除分组失败:', e.message); }
            if (watchGroupId.value === id) watchGroupId.value = null;
            await refreshWatch();
        }

        async function removeWatch(s) {
            if (!s) return;
            // 需求101506-2: 板块/指数与个股可能同 code, 优先用自选项主键 wlId 删除, 避免误删
            let id = s.wlId;
            if (id == null) {
                const grp = watchGroups.value.find(x => x.id === watchGroupId.value);
                const item = grp && grp.items.find(i => i.code === s.code && (i.kind || 'stock') === (s.wlKind || 'stock'));
                id = item && item.id;
            }
            if (id == null) return;
            try { await api('/watchlist/item/' + id, { method: 'DELETE' }); } catch(e) {}
            await refreshWatch();
        }

        // ---------- 需求101404-0: 表头字段取值 / 格式化 / 添加表头弹窗 ----------
        /** 字段取值: 仅返回本系统数据源可得的真实值, 其余返回 null (列内展示为 --) */
        function wlFieldValue(s, k) {
            if (!s) return null;
            const n = v => (typeof v === 'number' && isFinite(v)) ? v : null;
            const price = n(s.price), prev = n(s.prev_close);
            switch (k) {
                case 'change_pct': return n(s.change_pct);
                case 'price':      return price;
                case 'change':     return (price != null && prev != null) ? +(price - prev).toFixed(2) : null;
                case 'amplitude':  return (s.high != null && s.low != null && prev)
                    ? +(((s.high - s.low) / prev) * 100).toFixed(2) : null;
                case 'open':       return n(s.open);
                case 'high':       return n(s.high);
                case 'low':        return n(s.low);
                case 'prev_close': return prev;
                case 'volume':     return n(s.volume);
                case 'amount':     return n(s.amount);
                default:           return null;   // 量比/换手/现量/均笔额/买一/卖一 + 财务/资金/特色: 数据源暂不可得
            }
        }
        const WL_PCT_FIELDS = new Set(['change_pct', 'amplitude', 'turnover', 'speed',
            'dd_net_pct', 'profit_growth', 'roe', 'debt_ratio']);
        function wlFieldText(s, k) {
            const v = wlFieldValue(s, k);
            if (v == null) return '--';
            if (k === 'volume') return fmtVol(v);
            if (k === 'amount') return fmtMoney(v);
            if (WL_PCT_FIELDS.has(k)) return (v > 0 ? '+' : '') + v.toFixed(2) + '%';
            if (typeof v === 'number') return v.toFixed(2);
            return String(v);
        }
        function wlFieldClass(s, k) {
            let v = null;
            if (k === 'change_pct') v = s?.change_pct;
            else if (k === 'change') v = wlFieldValue(s, 'change');
            if (v == null || !isFinite(v) || v === 0) return '';
            return v > 0 ? 'up' : 'down';
        }
        function isWlText(k) { return WL_TEXT_FIELDS.has(k); }
        function wlFieldLabel(k) { return WL_FIELD_LABEL[k] || k; }

        function openWlHeader() {
            wlHeaderDraft.value = wlFields.value.slice();
            wlHeaderKw.value = '';
            wlCatOpen.value = { basic: true, finance: true, fund: true, special: true };
            wlHeaderOpen.value = true;
        }
        function cancelWlHeader() { wlHeaderOpen.value = false; wlHeaderKw.value = ''; }
        function toggleWlField(k) {
            const i = wlHeaderDraft.value.indexOf(k);
            if (i >= 0) wlHeaderDraft.value.splice(i, 1);
            else wlHeaderDraft.value.push(k);
        }
        function toggleWlCat(key) { wlCatOpen.value[key] = !wlCatOpen.value[key]; }
        function saveWlHeader() {
            const pick = new Set(wlHeaderDraft.value);
            const ordered = WL_FIELD_ALL.map(([k]) => k).filter(k => pick.has(k));
            wlFields.value = ordered;
            try { localStorage.setItem(WL_LS_KEY, JSON.stringify(ordered)); } catch (e) { /* ignore */ }
            wlHeaderOpen.value = false;
            wlHeaderKw.value = '';
        }
        function resetWlHeader() { wlHeaderDraft.value = WL_FIELD_DEFAULT.slice(); }

        // ---------- 需求101501-2: 图表模块左侧分组个股抽屉 ----------
        const drawerOpen = ref(true);
        function toggleDrawer() {
            drawerOpen.value = !drawerOpen.value;
            nextTick(() => { klineChart?.resize(); intradayChart?.resize(); });
        }

        // ---------- 分组个股行点击 ----------
        // 同一份列表在两处复用: 「自选」模块(模块级全宽, 单击仅选中, 双击才跳图表) /
        // 「图表」模块左侧抽屉(单击直接加载右侧K线, 双击打开个股详情弹层)
        // 用计时器自判单/双击, 避免选中后布局变化导致原生 dblclick 落空
        let wlClickTimer = null;
        let wlClickCode = null;
        function onWlRowClick(s) {
            if (!s) return;
            // 需求101506-2: 行可能是个股/板块/指数, 用 wlId 兜底 code 作为单击判定键
            const key = (s.wlId != null) ? ('W' + s.wlId)
                : ((s.isIndex ? 'I' : s.isBoard ? 'B' : 'S') + (s.code || ''));
            const inChart = activeTab.value === 'chart';
            if (wlClickTimer && wlClickCode === key) {                 // 双击
                clearTimeout(wlClickTimer); wlClickTimer = null; wlClickCode = null;
                if (inChart && !s.isBoard) openStockDetail(s); else openWlRow(s);   // 个股详情弹层仅个股
                return;
            }
            clearTimeout(wlClickTimer);
            wlClickCode = key;
            wlClickTimer = setTimeout(() => {
                wlClickTimer = null; wlClickCode = null;
                if (inChart) openWlRow(s);              // 图表: 单击直接加载 (个股/板块/指数)
                else selectedStock.value = s;           // 自选: 单击仅选中行, 不跳转
            }, 320);
        }
        // ---------- 需求101502-1: 图表模块列表 ↑/↓ 上下选择 → 右侧 K线/分时跟随切换 ----------
        function navWlRow(step) {
            const list = stocks.value || [];
            if (!list.length) return;
            const cur = selectedStock.value ? list.findIndex(s => s.code === selectedStock.value.code) : -1;
            const next = cur < 0 ? (step > 0 ? 0 : list.length - 1) : cur + step;
            const idx = Math.max(0, Math.min(list.length - 1, next));
            if (idx === cur) return;
            selectStock(list[idx]);
        }
        /** 需求101506-4: 用浏览器实时 hover 命中判定鼠标位置 (mouseenter 标志可能因滚动/覆盖层而失效) */
        function hoveringSel(sel) {
            const list = document.querySelectorAll(':hover');
            for (const el of list) {
                if (el.closest && el.closest(sel)) return true;
            }
            return false;
        }
        function onNavKey(e) {
            if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return;
            if (activeTab.value !== 'chart') return;
            const t = e.target || {};
            if (t.tagName === 'INPUT' || t.tagName === 'SELECT' || t.tagName === 'TEXTAREA' || t.isContentEditable) return;
            // 需求101506-4: ↑/↓ 默认缩放K线滑动条范围; 仅当悬停在左侧分组列表中时切换个股
            if (drawerOpen.value && hoveringSel('.sidebar') && !hoveringSel('#kline-chart')) {
                e.preventDefault();
                navWlRow(e.key === 'ArrowDown' ? 1 : -1);
                return;
            }
            if (klineChart) {
                e.preventDefault();
                window.KZoom(klineChart, e.key === 'ArrowUp' ? 'in' : 'out');
            }
        }
        function openStockDetail(s) {
            if (!s || !s.code || s.isBoard) return;
            sdStock.value = { ...s };
            sdPeriod.value = 'intraday';
            sdMsg.value = '';
            sdKline.value = [];
            sdBottomTab.value = '超级盘口';
            sdNewsTab.value = '新闻资讯';
            sdOpen.value = true;
            nextTick(() => sdLoadChart());
        }
        function closeStockDetail() { sdOpen.value = false; sdDisposeChart(); }
        function sdDisposeChart() { try { sdChart?.dispose(); } catch (e) {} sdChart = null; }
        async function sdSetPeriod(p) { sdPeriod.value = p; await sdLoadChart(); }
        async function sdLoadChart() {
            const s = sdStock.value;
            if (!s?.code) return;
            sdLoading.value = true;
            sdMsg.value = '';
            try {
                if (sdPeriod.value === 'intraday') {
                    const data = await api('/stocks/' + s.code + '/intraday');
                    if (data?.suspended || !data?.points?.length) {
                        sdDisposeChart();
                        sdMsg.value = data?.hint || '暂无真实分钟数据';
                        return;
                    }
                    await waitForSize('sd-chart');
                    sdDisposeChart();
                    sdChart = echarts.init(document.getElementById('sd-chart'));
                    drawIntradayInto(sdChart, data);
                } else {
                    const rows = await api('/stocks/' + s.code + '/kline?days=250&period=' + sdPeriod.value);
                    sdKline.value = rows || [];
                    if (!sdKline.value.length) { sdDisposeChart(); sdMsg.value = '暂无 K 线数据'; return; }
                    await waitForSize('sd-chart');
                    sdDisposeChart();
                    sdChart = echarts.init(document.getElementById('sd-chart'));
                    drawSdKline(sdKline.value);
                }
            } catch (e) {
                sdDisposeChart();
                sdMsg.value = '加载失败: ' + e.message;
            } finally {
                sdLoading.value = false;
            }
        }
        /** 详情右侧盘口字段 (图2): 数据源可得填真实值, 否则 -- */
        const sdQuote = computed(() => {
            const s = sdStock.value || {};
            const f = v => (typeof v === 'number' && isFinite(v)) ? v.toFixed(2) : '--';
            const prev = s.prev_close;
            const amp = (s.high != null && s.low != null && prev) ? ((s.high - s.low) / prev * 100) : null;
            const avg = (s.amount && s.volume) ? (s.amount / (s.volume * 100)) : null;
            return [
                ['昨收', f(prev)], ['今开', f(s.open)], ['最高', f(s.high)], ['最低', f(s.low)],
                ['内盘', '--'], ['外盘', '--'], ['换手', '--'], ['量比', '--'],
                ['总量', fmtVol(s.volume)], ['金额', fmtMoney(s.amount)],
                ['振幅', amp != null ? (amp > 0 ? '+' : '') + amp.toFixed(2) + '%' : '--'],
                ['均价', f(avg)],
                ['流通股', '--'], ['总股本', '--'], ['流通值', '--'], ['总市值', '--'],
                ['市盈TTM', '--'], ['市净率', '--'], ['行业市盈TTM', '--'], ['行业市净率', '--'],
                ['表决权差异', '--'], ['尚未盈利', '--'], ['是否注册制', '--'], ['VIE架构', '--'],
            ].map(([k, v]) => ({ k, v }));
        });

        // ---------- 选股票/板块 (核心入口) ----------
        function selectStock(s) {
            if (!s || !s.code) return;
            selectedStock.value = s;
            selectedKlineDate.value = null;
            intradayData.value = null;
            intradayAmp.value = null;
            if (!s.isBoard) boardStats.value = null;   // 切回个股时清掉板块指数展示
            // 注: 板块成分股列表 (boardActive) 不在此清除 —— 点成分股可继续留在板块列表里查看个股

            // 切到 图表 tab (如果不在) —— 个股/板块/指数行情统一在「图表」模块展示
            if (activeTab.value !== 'chart') switchTab('chart');

            // nextTick 后 init ECharts (此时 图表 tab 已经 v-show 为 visible)
            nextTick(async () => {
                await loadKline();
                await loadIntraday(null);
            });
        }

        // ---------- 需求5: 大盘指数 (看盘点击 → 图表模块展示) ----------
        async function loadIndexList() {
            if (indexList.value.length) return;
            try { indexList.value = (await api('/index/list')).indices || []; }
            catch(e) { console.warn('指数清单加载失败:', e.message); }
        }
        /** 选择大盘指数: 复用板块视图展示 (kline/intraday 走 /api/index/*) */
        function selectIndex(ix) {
            if (!ix || !ix.code) return;
            const meta = indexList.value.find(x => x.code === ix.code) || {};
            selectStock({
                code: ix.code, symbol: meta.symbol || null,
                name: meta.name || ix.name || ix.code,
                isBoard: true, isIndex: true, boardKind: 'index',
                price: ix.price ?? null, change_pct: ix.pct ?? null,
            });
        }

        // ---------- 加载 K 线 (个股 / 板块复合指数) ----------
        /** 需求101506-2: 当前图表标的对应的自选唯一键 (个股/板块/指数区分) */
        function currentWatchKey() {
            const s = selectedStock.value;
            if (!s) return null;
            if (s.isIndex) return 'index|' + (s.code || '');
            if (s.isBoard) return 'board|' + (s.boardKind || 'industry') + '|' + (s.label || s.name || '');
            return 'stock|' + (s.code || '');
        }
        /** 需求101506-2: 当前标的是否已在当前分组自选中 */
        const inWatch = computed(() => {
            const key = currentWatchKey();
            if (!key) return false;
            return (watchList.value || []).some(i => {
                const kind = i.kind || 'stock';
                let m = {};
                try { m = JSON.parse(i.meta || '{}') || {}; } catch(e) { m = {}; }
                if (kind === 'index') return 'index|' + (m.code || i.code || '') === key;
                if (kind === 'board') return 'board|' + (m.boardKind || 'industry') + '|' + (m.label || i.name || '') === key;
                return 'stock|' + (i.code || '') === key;
            });
        });
        /** 需求101506-2: 把当前标的(个股/板块/指数)加入当前自选分组 */
        async function addCurrentToWatch() {
            const s = selectedStock.value;
            if (!s) return;
            let body;
            if (s.isIndex) {
                body = { group_id: currentGroupId(), code: s.code, name: s.name, kind: 'index',
                         meta: JSON.stringify({ code: s.code, symbol: s.symbol || null }) };
            } else if (s.isBoard) {
                body = { group_id: currentGroupId(), code: s.label || s.name, name: s.name, kind: 'board',
                         meta: JSON.stringify({ boardKind: s.boardKind || 'industry', label: s.label || s.name }) };
            } else {
                body = { group_id: currentGroupId(), code: s.code, name: s.name, kind: 'stock' };
            }
            try { await api('/watchlist/item', { method: 'POST', body: JSON.stringify(body) }); }
            catch(e) { console.warn('加入自选失败:', e.message); }
            await refreshWatch();
        }
        /** 需求101508-3: 已自选则取消自选, 未自选则加入自选 */
        async function toggleCurrentWatch() {
            const s = selectedStock.value;
            if (!s) return;
            if (inWatch.value) {
                const key = currentWatchKey();
                const item = (watchList.value || []).find(i => {
                    const kind = i.kind || 'stock';
                    let m = {};
                    try { m = JSON.parse(i.meta || '{}') || {}; } catch(e) { m = {}; }
                    if (kind === 'index') return 'index|' + (m.code || i.code || '') === key;
                    if (kind === 'board') return 'board|' + (m.boardKind || 'industry') + '|' + (m.label || i.name || '') === key;
                    return 'stock|' + (i.code || '') === key;
                });
                if (!item) return;
                try { await api('/watchlist/item/' + item.id, { method: 'DELETE' }); }
                catch(e) { console.warn('取消自选失败:', e.message); }
                await refreshWatch();
            } else {
                await addCurrentToWatch();
            }
        }

        // ---------- 需求101508-2: 图表左侧个股列表的当日分时缩略图 ----------
        const sparkData = ref({});        // code -> [价格序列]
        let sparkLoadedAt = 0;
        /** 图表模式下涨幅列渲染分时缩略图 (其余模块/列保持文本) */
        function isSparkCol(f) { return activeTab.value === 'chart' && f === 'change_pct'; }
        /** 拉取当前列表个股的分时缩略图 (60s 内复用, 后端 limit=20) */
        async function loadSparks(force) {
            if (activeTab.value !== 'chart') return;
            if (!force && Date.now() - sparkLoadedAt < 60000) return;
            const codes = (stocks.value || []).filter(s => s && s.code && !s.isBoard && !s.isIndex)
                .map(s => s.code).slice(0, 20);
            if (!codes.length) { sparkData.value = {}; return; }
            try {
                const r = await api('/watchlist/sparklines?codes=' + codes.join(','));
                sparkData.value = (r && r.items) || {};
                sparkLoadedAt = Date.now();
            } catch(e) { console.warn('分时缩略图加载失败:', e.message); }
        }
        /** 生成 SVG polyline 坐标串 (viewBox 0 0 60 20, min-max 归一化) */
        function sparkPath(code) {
            const arr = (sparkData.value || {})[code];
            if (!arr || arr.length < 2) return '';
            let lo = Infinity, hi = -Infinity;
            for (const v of arr) { if (v < lo) lo = v; if (v > hi) hi = v; }
            const span = (hi - lo) || 1;
            const n = arr.length;
            return arr.map((v, i) => {
                const x = (i / (n - 1)) * 60;
                const y = 18 - ((v - lo) / span) * 16;
                return x.toFixed(1) + ',' + y.toFixed(1);
            }).join(' ');
        }
        /** 末点≥首点 → up(红), 否则 down(绿) */
        function sparkCls(code) {
            const arr = (sparkData.value || {})[code];
            if (!arr || arr.length < 2) return '';
            return arr[arr.length - 1] >= arr[0] ? 'up' : 'down';
        }
        // 图表模式下列表变化时自动补拉缩略图
        watch([stocks, activeTab], function () { if (activeTab.value === 'chart') loadSparks(true); });

        async function loadKline() {
            const sb = selectedStock.value;
            if (!sb?.code) return;
            try {
                const data = sb.isIndex
                    ? await api(`/index/${encodeURIComponent(sb.code)}/kline?days=1000&period=${klinePeriod.value}`)
                    : sb.isBoard
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
                const url = sb.isIndex
                    ? `/index/${encodeURIComponent(sb.code)}/intraday`
                      + (date ? `?date=${encodeURIComponent(date)}` : '')
                    : sb.isBoard
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
        function drawIntraday(data) { drawIntradayInto(intradayChart, data); }
        function drawIntradayInto(chart, data) {
            if (!chart || !data?.points?.length) return;
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

            chart.setOption({
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

        // ---------- 需求101404-1: 个股详情弹层 —— 紧凑 K 线 (蜡烛+MA5/10/20+量+缩放) ----------
        function sdMA(rows, n) {
            const out = [];
            let sum = 0;
            for (let i = 0; i < rows.length; i++) {
                sum += rows[i].close;
                if (i >= n) sum -= rows[i - n].close;
                out.push(i >= n - 1 ? +(sum / n).toFixed(2) : null);
            }
            return out;
        }
        function drawSdKline(rows) {
            if (!sdChart || !rows?.length) return;
            const dates = rows.map(r => r.date);
            const candle = rows.map(r => [r.open, r.close, r.low, r.high]);
            const vols = rows.map((r) => ({
                value: r.volume,
                itemStyle: { color: r.close >= r.open ? CHART.up : CHART.down },
            }));
            sdChart.setOption({
                backgroundColor: 'transparent', animation: false,
                tooltip: {
                    trigger: 'axis', axisPointer: { type: 'cross' },
                    formatter: (params) => {
                        const k = rows[params[0].dataIndex];
                        if (!k) return '';
                        return `${k.date}<br/>开 ${k.open?.toFixed?.(2)} 高 ${k.high?.toFixed?.(2)}`
                            + `<br/>低 ${k.low?.toFixed?.(2)} 收 ${k.close?.toFixed?.(2)}`
                            + `<br/>量 ${fmtVol(k.volume)}`;
                    },
                },
                legend: { data: ['MA5', 'MA10', 'MA20'], textStyle: { color: CHART.axisLabel, fontSize: 10 }, top: 0, right: 8 },
                grid: [
                    { left: 54, right: 16, top: 26, height: '60%' },
                    { left: 54, right: 16, top: '76%', height: '16%' },
                ],
                xAxis: [
                    { type: 'category', data: dates, boundaryGap: true,
                      axisLine: { lineStyle: { color: CHART.axisLine } },
                      axisLabel: { color: CHART.axisLabel, fontSize: 9 }, gridIndex: 0 },
                    { type: 'category', data: dates, axisLine: { show: false },
                      axisLabel: { show: false }, gridIndex: 1 },
                ],
                yAxis: [
                    { scale: true, axisLine: { show: false }, splitLine: { lineStyle: { color: CHART.grid } },
                      axisLabel: { color: CHART.axisLabel, fontSize: 9 }, gridIndex: 0 },
                    { scale: true, axisLine: { show: false }, splitLine: { show: false },
                      axisLabel: { color: CHART.axisLabel3, fontSize: 9 }, gridIndex: 1 },
                ],
                dataZoom: [
                    { type: 'inside', xAxisIndex: [0, 1], start: 60, end: 100 },
                    { type: 'slider', xAxisIndex: [0, 1], bottom: 2, height: 14, start: 60, end: 100,
                      borderColor: CHART.axisLine, fillerColor: CHART.sliderFill,
                      backgroundColor: CHART.sliderBg, textStyle: { color: CHART.axisLabel, fontSize: 9 } },
                ],
                series: [
                    { name: 'K', type: 'candlestick', data: candle, xAxisIndex: 0, yAxisIndex: 0,
                      itemStyle: { color: CHART.up, color0: CHART.down, borderColor: CHART.up, borderColor0: CHART.down } },
                    { name: 'MA5', type: 'line', data: sdMA(rows, 5), smooth: true, showSymbol: false,
                      lineStyle: { width: 1, color: '#ffaa00' }, xAxisIndex: 0, yAxisIndex: 0 },
                    { name: 'MA10', type: 'line', data: sdMA(rows, 10), smooth: true, showSymbol: false,
                      lineStyle: { width: 1, color: '#00b5ff' }, xAxisIndex: 0, yAxisIndex: 0 },
                    { name: 'MA20', type: 'line', data: sdMA(rows, 20), smooth: true, showSymbol: false,
                      lineStyle: { width: 1, color: '#b45bff' }, xAxisIndex: 0, yAxisIndex: 0 },
                    { name: '成交量', type: 'bar', data: vols, xAxisIndex: 1, yAxisIndex: 1 },
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
        // 需求101804-1: 分组删除改由「分组标签上的 ✕」承担 (静态走 canDelGroupById / delGroupById,
        // 动态走 deleteDynGroup, 内置预设不可删); 原右上角「🗑 删除当前分组」入口已移除

        async function loadDynGroups() {
            if (!isLoggedIn.value) { dynGroups.value = []; return; }
            try {
                dynGroups.value = (await api('/dyn-groups')).groups || [];
            } catch(e) { console.warn('动态分组加载失败:', e.message); dynGroups.value = []; }
        }

        async function loadDynMetrics() {
            if (!dynMetrics.value.length) {
                try { dynMetrics.value = (await api('/dyn-groups/metrics')).metrics || []; }
                catch(e) { console.warn('指标元数据加载失败:', e.message); }
            }
            await loadDynRecent();
        }

        /** 「最近使用」指标: 按账号持久化, 后端在保存 / 预览时记录 */
        async function loadDynRecent() {
            if (!isLoggedIn.value) { dynRecent.value = []; return; }
            try { dynRecent.value = (await api('/dyn-groups/recent-metrics')).metrics || []; }
            catch(e) { dynRecent.value = []; }
        }

        /** 指标分类 (按注册表顺序去重; 缺省归入「其他」) */
        const dynCats = computed(() => {
            const out = [];
            dynMetrics.value.forEach(m => {
                const c = m.cat || '其他';
                if (!out.includes(c)) out.push(c);
            });
            return out;
        });
        const dynTabs = computed(() => ['最近使用'].concat(dynCats.value));
        const dynTabMetrics = computed(() => {
            if (dynTab.value !== '最近使用') {
                return dynMetrics.value.filter(m => (m.cat || '其他') === dynTab.value);
            }
            const byKey = {};
            dynMetrics.value.forEach(m => { byKey[m.key] = m; });
            return dynRecent.value.map(k => byKey[k]).filter(Boolean);
        });
        /** 底部主按钮文案: 查看过则带上命中数 (对标图片「查看结果(56只)」) */
        const dynPreviewLabel = computed(() =>
            dynPreviewTotal.value == null ? '查看结果' : '查看结果(' + dynPreviewTotal.value + '只)');

        /** 需求101502-0: 动态分组结果缓存 (按分组 id) —— 取到后复用, 仅点分组内「刷新」按钮重新获取 */
        const dynStocksCache = {};

        /** 动态分组个股: 已缓存则直接复用; force=true(点刷新 / 改规则后)才重新求值并覆盖缓存 */
        async function loadDynStocks(id, force = false) {
            if (id == null) return;
            if (!force && dynStocksCache[id]) { stocks.value = dynStocksCache[id]; return; }
            dynLoading.value = true;
            try {
                const r = await api('/dyn-groups/' + id + '/stocks?limit=200' + (force ? '&refresh=1' : ''));
                stocks.value = r.stocks || [];
                dynStocksCache[id] = stocks.value;
            } catch(e) {
                console.warn('动态分组获取失败:', e.message);
                stocks.value = dynStocksCache[id] || [];
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
        function dynLabel(key) { const m = dynMetricMeta(key); return m ? m.label : key; }
        function dynMetricSelected(key) { return dynConds.value.some(c => c.metric === key); }
        function newCond(metricKey) {
            const m = metricKey ? dynMetricMeta(metricKey) : dynMetrics.value[0];
            return {
                metric: m ? m.key : 'is_limit_up',
                op: m && m.type === 'bool' ? 'eq' : 'gt',
                value: 0, value1: 0, value2: 0, boolVal: true,
            };
        }
        /** 指标网格点选: 已选则移除, 未选则加入已选条件 (同一指标不重复) */
        function toggleDynMetric(key) {
            if (dynEditBuiltin.value) return;
            const i = dynConds.value.findIndex(c => c.metric === key);
            if (i >= 0) dynConds.value.splice(i, 1);
            else dynConds.value.push(newCond(key));
            dynPreviewTotal.value = null;
            dynPreviewMsg.value = '';
        }
        function removeDynCond(i) {
            dynConds.value.splice(i, 1);
            dynPreviewTotal.value = null;
            dynPreviewMsg.value = '';
        }

        /** 打开规则编辑器: 传 g=已有分组(查看/编辑), 省略=新建 */
        async function openDynEditor(g) {
            await loadDynMetrics();
            if (!dynMetrics.value.length) return;
            dynPreviewMsg.value = '';
            dynPreviewTotal.value = null;
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
            // 默认落在「最近使用」; 无记录时落到第一个分类 (对标图片 Tab 顺序)
            dynTab.value = dynRecent.value.length ? '最近使用' : (dynCats.value[0] || '最近使用');
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
                dynPreviewTotal.value = r.total;
                dynPreviewMsg.value = '';
                await loadDynRecent();      // 预览即算「使用」→ 刷新最近使用
            } catch(e) {
                dynPreviewTotal.value = null;
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
                await loadDynRecent();      // 保存即算「使用」→ 刷新最近使用
                if (isDynSelection.value) await loadDynStocks(currentDynGroupId.value, true);
            } catch(e) {
                let msg = e.message; try { msg = JSON.parse(e.message).reason || msg; } catch(_) {}
                dynPreviewMsg.value = '保存失败: ' + String(msg).replace(/^"|"$/g, '');
            } finally { dynSaving.value = false; }
        }

        async function deleteDynGroup(id) {
            try { await api('/dyn-groups/' + id, { method: 'DELETE' }); }
            catch(e) { console.warn('删除动态分组失败:', e.message); }
            // 需求101804-1: 删除的是非当前分组时不改变当前选择
            if (watchGroupId.value === dynKey(id)) watchGroupId.value = null;
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
            if (selectedStock.value && activeTab.value === 'chart') {
                await loadKline();
                if (klineDataset.value.length) await loadIntraday(selectedKlineDate.value);
            }
        }

        // ---------- 初始化 ----------
        onMounted(async () => {
            applyTheme(theme.value);        // 应用已记忆的页面风格 (黑/白)
            // 恢复登录态: 拉取 /auth/me 获取手机号绑定状态 (旧账号需补绑)
            // 需求101607: 登录态决定 isLoggedIn, 必须先行; 其余三项相互独立 → 并发发起缩短首屏就绪时间
            await refreshMe();
            maybeOnboard();          // 需求101801-0: 已登录且未看过引导 → 首次登录展示核心流程
            await Promise.allSettled([
                refreshWatch(),      // 加载自选分组 (后端持久化, 默认分组「自选」; 未登录返回空)
                loadDynGroups(),
                loadIndexList(),     // 需求5: 指数清单 (看盘跳转用)
            ]);
            loadAccount();           // 账户信息异步填充, 不阻塞自选/图表首屏

            // 默认选第一个自选股 (如果有)
            if (stocks.value.length) {
                selectStock(stocks.value[0]);
            }

            // 窗口 resize / 手机横竖屏切换 → resize ECharts
            const resizeCharts = () => { klineChart?.resize(); intradayChart?.resize(); sdChart?.resize(); };
            window.addEventListener('resize', resizeCharts);
            window.addEventListener('orientationchange', () => setTimeout(resizeCharts, 300));

            // 需求101503-0: 预取「本地映射数据」(股票注册表 + 板块), 供两个模块的搜索离线匹配
            // 需求101607: 首屏不阻塞 → 空闲时再拉取, 命中缓存则立即执行
            if (window.requestIdleCallback) requestIdleCallback(() => ensureLocalIndex(), { timeout: 2000 });
            else setTimeout(ensureLocalIndex, 300);

            // 需求101502-1: 「图表」模块左侧列表 ↑/↓ 上下选择
            window.addEventListener('keydown', onNavKey);

            // 行情轮询: 每 15s 刷新自选列表与选中标的的最新价/涨跌幅 (板块视图跳过)
            // 需求101607: 页面隐藏(切标签/后台)时跳过轮询, 减少无谓请求与重绘
            setInterval(async () => {
                if (document.hidden) return;
                await refreshQuotes();
            }, 15000);

            console.log('✅ App mounted, Console 零错误 = 架构正确');
        });

        onUnmounted(() => {
            window.removeEventListener('resize', () => {});
            window.removeEventListener('keydown', onNavKey);
            if (wlClickTimer) { clearTimeout(wlClickTimer); wlClickTimer = null; }
            disposeCharts();
        });

        // 周期切换后需要重绘 (klinePeriod 变化时 loadKline 已重绘)

        // 供其它组件 (如模拟交易下单面板信息卡) 跳转行情页: 不加入任何自选/分类
        window.__gotoStock = selectStock;

        // 需求5: 看盘模块 (watch.js 独立组件) 点击指数/板块/个股 → 跳转「图表」模块展示
        window.__openChart = {
            index: (code, name, price, pct) => selectIndex({ code, name, price, pct }),
            stock: (code, name) => selectStock({ code, name, price: 0, market: marketOf(code) }),
            board: async (name) => {
                try {
                    const r = await api('/boards/search?kw=' + encodeURIComponent(name) + '&limit=1');
                    const b = (r.boards || [])[0];
                    if (b) selectBoard({ name: b.name, label: b.label }, b.kind);
                } catch(e) { console.warn('板块跳转失败:', e.message); }
            },
        };

        // ---------- return (暴露给模板) ----------
        return {
            // data
            tabs, activeTab,
            boardOpen, boardKind, boards, boardLoading, boardName, boardStocks, boardConsLoading,
            boardStats, boardSortedAsc, toggleBoardSort, boardActive,
            boardKw, boardSearching, boardSearchResults, boardList, boardListCount,
            debounceBoardSearch, clearBoardSearch,
            toggleBoard, switchBoardKind, loadBoards, selectBoard, selectBoardStock,
            selectBoardFromSearch,
            boardSel, boardAddGroupId, boardAdding, boardAllSel,
            toggleBoardSel, toggleBoardAll, batchAddBoardStocks,
            searchKw, searchResults,
            watchList, stocks, watchGroups, watchGroupId, newGroupName, watchTitle,
            NONE_GROUP, watchOverview, grpPct, loadWatchOverview,
            newGroupOpen, newStaticOpen, toggleNewGroup, startNewStatic, startNewDyn,
            switchGroup, addGroup, delGroupById, canDelGroupById, removeWatch, reloadWatchView,
            // 动态分组 (按指标实时获取)
            dynGroups, dynKey, isDynSelection, currentDynGroup, deleteDynGroup, dynLoading,
            dynEditorOpen, dynEditId, dynEditBuiltin, dynName, dynConds, dynMetrics,
            dynPreviewMsg, dynSaving, dynTab, dynTabs, dynTabMetrics, dynPreviewLabel,
            dynLabel, dynMetricSelected,
            openDynEditor, toggleDynMetric, removeDynCond, dynOpsOf, dynIsBool, dynUnit,
            opLabel, previewDyn, saveDyn,
            // 设置模块 / 页面样式
            settingsOpen, settingsTab, openSettings, theme, setTheme,
            // 需求101801: 首次登录引导 + 操作手册(文档集)
            onboardOpen, onboardStep, onboardSteps, onboardGo, onboardGoTab, closeOnboard, maybeOnboard,
            tabTip,
            manualDocs, manualDocId, manualDoc,
            importFile, triggerImport, onImportFile,
            selectedStock, selectedKlineDate, indexList, selectIndex, loadIndexList,
            inWatch, addCurrentToWatch, toggleCurrentWatch,
            isSparkCol, sparkPath, sparkCls, loadSparks,
            klineDataset, intradayData, intradayLoading, intradayAmp,
            klinePeriods, klinePeriod,
            acct, tiers, profit,
            user, authMode, authUser, authPass, authMsg, authLoading, authSubmit, logout,
            authPhone, authEmail, authShowPass, needBindPhone, bindPhoneSubmitting,
            isLoggedIn, submitBindPhone, refreshMe, validPhone,
            // 需求101404-0: 自选表头 (字段目录 / 动态列 / 添加表头弹窗)
            wlFields, wlActiveFields, wlCols, WL_FIELD_CATS, wlHeaderCats,
            wlHeaderOpen, wlHeaderKw, wlHeaderDraft, wlCatOpen,
            openWlHeader, cancelWlHeader, toggleWlField, toggleWlCat, saveWlHeader, resetWlHeader,
            wlFieldText, wlFieldClass, isWlText, wlFieldLabel,
            // 需求101404-1: 双击个股详情弹层
            sdOpen, sdStock, sdPeriod, sdPeriods, sdLoading, sdMsg, sdKline, sdQuote,
            sdBottomTabs, sdBottomTab, sdNewsTabs, sdNewsTab,
            openStockDetail, closeStockDetail, sdSetPeriod, onWlRowClick,
            // 需求101501-0/2: 自选模块级展示 + 图表左侧分组抽屉
            drawerOpen, toggleDrawer,
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
if (window.WatchPane) app.component('watch-pane', window.WatchPane);

app.mount('#app');

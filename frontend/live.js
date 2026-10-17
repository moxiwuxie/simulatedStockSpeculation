/**
 * 模拟交易 (实时撮合) —— 组件库
 *
 * 导出:
 *   window.LivePane      —— 模拟交易页 (资金体系 / 资产 / 持仓明细 / 委托 / 成交)
 *   window.TradeOrderBox —— 常驻下单面板 (模拟交易页 + 行情页复用)
 *
 * 说明:
 * - 下单面板「常驻」展示且不绑定当前页面标的: 面板内自带代码/名称搜索 + 行情信息卡,
 *   点击信息卡跳转到行情页 (不加入任何自选/分类)。
 * - 买卖、仓位、数量对齐训练模块; 并支持止盈/止损条件单 (触发即尝试操作)。
 *
 * 约束: 组件内不创建 ECharts; 用 v-show 不用 template v-if
 */
(function () {
  'use strict';
  const { ref, computed, watch, onUnmounted } = Vue;

  async function api(path, opts) {
    // 需求101602: 统一走预加载/缓存层 (只读GET缓存 + 并发去重), 见 preload.js
    return window.__apiFetch(path, opts);
  }

  const money = function (x) {
    if (x == null || isNaN(x)) return '--';
    const n = Number(x);
    if (Math.abs(n) >= 1e8) return (n / 1e8).toFixed(2) + '亿';
    if (Math.abs(n) >= 1e4) return (n / 1e4).toFixed(2) + '万';
    return n.toFixed(2);
  };
  const signClass = function (v) { return v > 0 ? 'up' : v < 0 ? 'down' : ''; };

  // 跳转行情页 (不加入自选/分类): 优先用 app.js 暴露的入口, 否则派发事件
  function gotoStock(stock) {
    if (typeof window.__gotoStock === 'function') return window.__gotoStock(stock);
    window.dispatchEvent(new CustomEvent('goto-stock', { detail: stock }));
  }

  const RATIOS = [
    { v: 1, label: '满仓' },
    { v: 0.5, label: '1/2' },
    { v: 1 / 3, label: '1/3' },
    { v: 0.2, label: '1/5' },
    { v: 0.1, label: '1/10' },
  ];

  const COND_RATIOS = [
    { v: 1, label: '全部仓位' },
    { v: 0.5, label: '一半仓位' },
    { v: 0.333, label: '三分之一' },
    { v: 0.2, label: '五分之一' },
  ];

  // ============================================================
  // 下单面板 (常驻, 自带标的搜索; 行情页 + 模拟交易页复用)
  // ============================================================
  window.TradeOrderBox = {
    name: 'trade-order-box',
    props: {
      code: { type: String, default: '' },
      active: { type: Boolean, default: true },
      title: { type: String, default: '模拟交易下单' },
    },
    template: `
<div class="trade-box">
  <div class="tb-head">
    <span class="tb-title">💹 {{ title }}</span>
    <span class="tb-quote" v-if="quote">
      {{ quote.name }} {{ quote.code }} · 现价
      <b :class="signClass(quote.change_pct)">¥{{ quote.price }}</b>
      <b :class="signClass(quote.change_pct)">{{ quote.change_pct }}%</b>
    </span>
    <span class="tb-quote" v-else>— 输入代码/名称选择标的 —</span>
  </div>

  <!-- 标的搜索 (常驻) -->
  <div class="tb-search">
    <input class="tb-input grow" v-model="kw"
           placeholder="输入股票代码或名称 (如 600519 / 贵州茅台)"
           @input="onKwInput" @focus="showResults = searchResults.length > 0"
           @keyup.enter="pickFirst" @keyup.esc="showResults = false">
    <button v-if="orderCode" class="btn mini ghost" @click="clearCode" title="清空标的">✕</button>
    <div class="tb-suggest" v-if="showResults && searchResults.length">
      <div v-for="r in searchResults" :key="r.code" class="tb-suggest-item" @click="pick(r)">
        <span class="s-name">{{ r.name }}</span>
        <span class="s-code">{{ r.code }}</span>
        <span class="s-pct" :class="signClass(r.change_pct)">{{ r.change_pct != null ? r.change_pct + '%' : '--' }}</span>
      </div>
    </div>
  </div>

  <!-- 行情信息卡 (点击跳转行情, 不加入自选/分类) -->
  <div class="tb-card" v-if="quote" @click="gotoQuote(quote)" title="点击查看该股行情">
    <span class="c-name">{{ quote.name }}</span>
    <span class="c-code">{{ quote.code }}</span>
    <span class="c-price" :class="signClass(quote.change_pct)">¥{{ quote.price }}</span>
    <span class="c-pct" :class="signClass(quote.change_pct)">{{ quote.change_pct }}%</span>
    <span class="c-go">查看行情 ›</span>
  </div>

  <div class="tb-body">
    <div class="tb-row" v-if="!quote">
      <span class="tb-tip">常驻下单面板 · 选择或输入标的后即可下单 (点击上方持仓行可快速填充)</span>
    </div>
    <div class="tb-row">
      <span class="tb-label">方向</span>
      <button :class="['btn','mini',{active: side==='BUY', buy: side==='BUY'}]" @click="side='BUY'">买入</button>
      <button :class="['btn','mini',{active: side==='SELL', sell: side==='SELL'}]" @click="side='SELL'">卖出</button>
      <span class="tb-label">类型</span>
      <button :class="['btn','mini',{active: otype==='MARKET'}]" @click="otype='MARKET'">市价</button>
      <button :class="['btn','mini',{active: otype==='LIMIT'}]" @click="otype='LIMIT'">限价</button>
    </div>

    <div class="tb-row">
      <span class="tb-label">仓位</span>
      <span v-for="r in ratios" :key="r.label"
            :class="['chip', 'mini', {active: chipActive(r.v)}]"
            @click="pickRatio(r.v)">{{ r.label }}</span>
      <span class="tb-label">自定义</span>
      <input class="tb-input sm" type="number" min="0" max="100" step="1"
             v-model="posPct" placeholder="仓位 %">
      <span class="tb-unit">%</span>
    </div>

    <div class="tb-row">
      <span class="tb-label">{{ otype === 'LIMIT' ? '委托价' : '参考价' }}</span>
      <input class="tb-input" type="number" step="0.01" v-model.number="price"
             :disabled="otype === 'MARKET'">
      <span class="tb-label">数量</span>
      <input class="tb-input" type="number" step="100" v-model="qty"
             placeholder="留空则按仓位">
      <span class="tb-tip" v-if="quote">限价区间 {{ quote.limit_down }} ~ {{ quote.limit_up }}</span>
    </div>

    <div class="tb-row tb-summary">
      <span class="tb-tip">
        {{ side === 'BUY' ? '可买' : '可卖' }}
        <b>{{ side === 'BUY' ? maxBuyQty : availSell }}</b> 股 ·
        预计数量 <b>{{ estQty }}</b> 股 ·
        预计金额 <b>¥{{ money(estAmount) }}</b>
      </span>
    </div>

    <div class="tb-row">
      <span class="tb-tip">
        上一交易日({{ quote?.basis_date || '--' }})成交额 {{ money(quote?.basis_amount) }} · 单笔上限
        <b>¥{{ money(quote?.max_order_amount) }}</b> (成交额的 5%)
      </span>
      <span v-if="side === 'BUY' && quote?.at_limit_up" class="tb-tip warn">⚠️ 已封涨停，无法买入</span>
      <button class="btn primary"
              :disabled="loading || !orderCode || (side === 'BUY' && quote?.at_limit_up)"
              @click="submit">
        {{ loading ? '提交中...' : (side === 'BUY' ? '买入' : '卖出') }}
      </button>
    </div>
  </div>

  <!-- 条件单 (止盈/止损): 不要求先有持仓, 触发即尝试操作 -->
  <div class="tb-cond">
    <div class="tb-row">
      <span class="tb-label">条件单</span>
      <select v-model="condKind" class="tb-select">
        <option value="take_profit">止盈</option>
        <option value="stop_loss">止损</option>
      </select>
      <input class="tb-input sm" type="number" step="0.01" v-model.number="condPrice" placeholder="触发价">
      <select v-model.number="condRatio" class="tb-select">
        <option v-for="r in condRatios" :key="r.label" :value="r.v">{{ r.label }}</option>
      </select>
      <button class="btn mini" @click="setCond">＋ 挂条件单</button>
    </div>
    <div class="tb-tip">触发价按实时价判定, 触及即尝试卖出; 无可卖持仓时给出提示</div>
    <div class="cond-list" v-if="conds.length">
      <div v-for="c in conds" :key="c.cond_id" class="cond-item" :class="c.kind">
        <span>{{ c.name }} {{ c.kind_label }} @{{ c.trigger_price }} · 平 {{ Math.round(c.ratio * 100) }}%</span>
        <span v-if="c.status === 'active'">
          <button class="cond-del" @click="cancelCond(c.cond_id)" title="撤销">✕</button>
        </span>
        <span v-else-if="c.status === 'triggered'" class="ok">✅ 已触发 @{{ c.triggered_price }} ({{ c.triggered_qty }} 股)</span>
        <span v-else-if="c.status === 'skipped'" class="warn">⚠️ {{ c.message }}</span>
        <span v-else class="muted">已撤销</span>
      </div>
    </div>
  </div>

  <div v-if="msg" class="tb-msg">{{ msg }}</div>
</div>
`,
    setup(props) {
      const quote = ref(null);
      const acct = ref({ available_cash: 0, positions: [] });
      const side = ref('BUY');
      const otype = ref('MARKET');
      const price = ref(0);
      const qty = ref('');
      const ratio = ref(null);       // chips 仓位 (0-1)
      const posPct = ref('');        // 自定义仓位 % (字符串)
      const msg = ref('');
      const loading = ref(false);

      // 标的搜索
      const kw = ref('');
      const orderCode = ref(props.code || '');
      const searchResults = ref([]);
      const showResults = ref(false);
      let searchTimer = null;

      // 条件单
      const conds = ref([]);
      const condKind = ref('take_profit');
      const condPrice = ref(null);
      const condRatio = ref(1);

      const pxNow = computed(() =>
        otype.value === 'LIMIT' ? Number(price.value || 0) : Number(quote.value?.price || 0));

      const position = computed(() => {
        const list = acct.value.positions || [];
        return list.find((p) => p.code === orderCode.value) || null;
      });
      const availSell = computed(() => (position.value ? position.value.available : 0));
      const maxBuyQty = computed(() => {
        const px = pxNow.value;
        if (px <= 0) return 0;
        return Math.floor((acct.value.available_cash || 0) / px / 100) * 100;
      });

      // 有效仓位比例: 自定义 % 优先于 chips
      const effFraction = computed(() => {
        if (posPct.value !== '' && posPct.value != null) {
          return Math.min(Math.max(Number(posPct.value) || 0, 0), 100) / 100;
        }
        return ratio.value;
      });

      // 预计数量: 手填数量优先, 否则按仓位换算
      const estQty = computed(() => {
        if (qty.value !== '' && qty.value != null && Number(qty.value) > 0) return Number(qty.value);
        const f = effFraction.value;
        if (f == null || f <= 0) return 0;
        if (side.value === 'BUY') return Math.floor((maxBuyQty.value * f) / 100) * 100;
        return Math.floor((availSell.value * f) / 100) * 100;
      });
      const estAmount = computed(() => Math.round(pxNow.value * estQty.value * 100) / 100);

      function chipActive(v) { return posPct.value === '' && ratio.value === v; }

      function pickRatio(v) {
        ratio.value = (ratio.value === v) ? null : v;
        if (ratio.value !== null) { posPct.value = ''; qty.value = ''; }
      }

      // 互斥: 手改自定义% → 取消 chips; 手填数量 → 清空仓位选择
      watch(posPct, (v) => { if (v !== '' && v != null) ratio.value = null; });
      watch(qty, (v) => {
        if (v !== '' && v != null && Number(v) > 0) { ratio.value = null; posPct.value = ''; }
      });

      // ---------- 标的搜索 ----------
      function onKwInput() {
        if (searchTimer) clearTimeout(searchTimer);
        const k = (kw.value || '').trim();
        if (!k) { searchResults.value = []; showResults.value = false; return; }
        searchTimer = setTimeout(async () => {
          try {
            const list = await api('/stocks/search?keyword=' + encodeURIComponent(k) + '&limit=8');
            searchResults.value = list || [];
            showResults.value = searchResults.value.length > 0;
          } catch (e) { searchResults.value = []; }
        }, 250);
      }

      function pick(r) {
        if (!r || !r.code) return;
        orderCode.value = r.code;
        kw.value = (r.name || '') + ' ' + r.code;
        searchResults.value = []; showResults.value = false;
        loadQuote();
      }
      function pickFirst() { if (searchResults.value.length) pick(searchResults.value[0]); }
      function clearCode() {
        orderCode.value = ''; kw.value = ''; quote.value = null;
        searchResults.value = []; showResults.value = false; msg.value = '';
      }

      // 外部填充标的 (如模拟交易页点击持仓行) —— 已选中同一标的时也强制刷新
      function fill(code, name) {
        if (!code) return;
        orderCode.value = code;
        kw.value = name ? (name + ' ' + code) : code;
        searchResults.value = []; showResults.value = false;
        loadQuote();
      }

      // 点击信息卡 → 跳转行情页 (不加入自选/分类)
      function gotoQuote(q) {
        if (!q) return;
        gotoStock({ code: q.code, name: q.name, price: q.price, change_pct: q.change_pct });
      }

      async function loadQuote() {
        msg.value = '';
        if (!orderCode.value) { quote.value = null; return; }
        try {
          const q = await api('/live/quote/' + orderCode.value);
          quote.value = q;
          price.value = q.price;
        } catch (e) {
          quote.value = null;
          msg.value = '❌ ' + e.message;
        }
        try { acct.value = await api('/live/account'); } catch (e) {}
      }

      async function submit() {
        msg.value = '';
        if (!orderCode.value) { msg.value = '请先选择标的'; return; }
        const body = {
          code: orderCode.value, side: side.value, type: otype.value,
          price: otype.value === 'LIMIT' ? Number(price.value) : 0,
        };
        if (qty.value !== '' && qty.value != null && Number(qty.value) > 0) {
          body.quantity = Number(qty.value);
        } else if (effFraction.value != null && effFraction.value > 0) {
          body.ratio = effFraction.value;
        } else {
          msg.value = '❌ 请填写数量或选择仓位';
          return;
        }
        loading.value = true;
        try {
          const o = await api('/live/orders', { method: 'POST', body: JSON.stringify(body) });
          msg.value = o.status === 'FILLED'
            ? `✅ 已成交 ${o.quantity} 股 @ ¥${o.filled_price} (手续费 ¥${o.fee})`
            : `⏳ 已挂单 (状态 ${o.status})，价格触及后自动成交`;
          qty.value = ''; posPct.value = ''; ratio.value = null;
          await loadQuote();
          await loadConds();
        } catch (e) {
          msg.value = '❌ ' + e.message;
        } finally {
          loading.value = false;
        }
      }

      // ---------- 条件单 ----------
      async function loadConds() {
        try { conds.value = (await api('/live/conditionals')).conditionals || []; }
        catch (e) { /* 未登录等场景静默 */ }
      }

      async function setCond() {
        if (!orderCode.value) { msg.value = '请先选择标的'; return; }
        const tp = Number(condPrice.value);
        if (!tp || tp <= 0) { msg.value = '请输入有效的触发价'; return; }
        try {
          await api('/live/conditionals', {
            method: 'POST',
            body: JSON.stringify({ code: orderCode.value, kind: condKind.value,
                                   trigger_price: tp, ratio: condRatio.value || 1 }),
          });
          condPrice.value = null; msg.value = '✅ 已挂条件单 (触发即尝试卖出)';
          await loadConds();
        } catch (e) { msg.value = '❌ ' + e.message; }
      }

      async function cancelCond(id) {
        try {
          await api('/live/conditionals/' + id, { method: 'DELETE' });
          await loadConds();
        } catch (e) { msg.value = '❌ ' + e.message; }
      }

      watch(() => props.code, (v) => { if (v) { orderCode.value = v; loadQuote(); } });

      let timer = null;
      function startPoll() {
        stopPoll();
        timer = setInterval(() => { loadConds(); }, 4000);
      }
      function stopPoll() { if (timer) { clearInterval(timer); timer = null; } }

      watch(() => props.active, function (v) {
        if (v) { loadQuote(); loadConds(); startPoll(); }
        else { stopPoll(); }
      }, { immediate: true });

      onUnmounted(stopPoll);

      return { quote, acct, side, otype, price, qty, ratio, posPct, msg, loading,
               ratios: RATIOS, condRatios: COND_RATIOS, chipActive, pickRatio, submit,
               money, signClass, availSell, maxBuyQty, estQty, estAmount,
               kw, orderCode, searchResults, showResults, onKwInput, pick, pickFirst,
               clearCode, fill, gotoQuote, conds, condKind, condPrice, condRatio, setCond, cancelCond };
    },
  };

  // ============================================================
  // 模拟交易页
  // ============================================================
  window.LivePane = {
    name: 'live-pane',
    props: { active: { type: Boolean, default: false } },
    template: `
<div class="live-wrap">
  <div class="lw-card">
    <div class="lw-head">
      <div class="cat-chips">
        <span v-for="t in tiers" :key="t.key"
              :class="['chip', {active: acct.tier === t.key}]"
              @click="bindTier(t.key)">
          {{ t.name }} ¥{{ (t.initial_cash / 10000) }}万
        </span>
      </div>
      <span class="lw-right">
        <span class="lw-date" v-if="acct.trade_date">交易日 {{ acct.trade_date }}</span>
        <button class="btn mini ghost" @click="loadAll">刷新</button>
        <button class="btn mini danger" @click="resetAccount">重置账户</button>
      </span>
    </div>

    <div class="lw-cards">
      <div class="lw-cell"><span>总资产</span><b>{{ money(acct.total_assets) }}</b></div>
      <div class="lw-cell"><span>可用资金</span><b class="v">{{ money(acct.available_cash) }}</b></div>
      <div class="lw-cell"><span>冻结资金</span><b>{{ money(acct.frozen_cash) }}</b></div>
      <div class="lw-cell"><span>持仓市值</span><b>{{ money(acct.market_value) }}</b></div>
      <div class="lw-cell"><span>浮动盈亏</span>
        <b :class="signClass(acct.float_pnl)">{{ money(acct.float_pnl) }}</b></div>
      <div class="lw-cell"><span>累计盈亏</span>
        <b :class="signClass(acct.total_pnl)">{{ money(acct.total_pnl) }} ({{ acct.total_pnl_pct }}%)</b></div>
    </div>
  </div>

  <div class="lw-card">
    <div class="lw-card-title">💼 持仓明细
      <span class="sub">T+1: 当日买入的「不可用」股数次日可卖</span>
    </div>
    <div v-if="!acct.positions || !acct.positions.length" class="lw-empty">暂无持仓</div>
    <table v-else class="rv-table">
      <thead><tr>
        <th>代码</th><th>名称</th><th>持仓</th><th>可用</th><th>不可用</th>
        <th>成本</th><th>现价</th><th>市值</th><th>浮动盈亏</th>
      </tr></thead>
      <tbody>
        <tr v-for="p in acct.positions" :key="p.code"
            @click="fillPosition(p)" title="点击填充至下单面板">
          <td>{{ p.code }}</td>
          <td>{{ p.name }}</td>
          <td>{{ p.quantity }}</td>
          <td class="up">{{ p.available }}</td>
          <td class="down">{{ p.pending_in }}</td>
          <td>{{ p.avg_cost }}</td>
          <td>{{ p.price }}</td>
          <td>{{ money(p.market_value) }}</td>
          <td :class="signClass(p.profit)">{{ money(p.profit) }} ({{ p.profit_pct }}%)</td>
        </tr>
      </tbody>
    </table>
  </div>

  <div class="lw-card">
    <div class="lw-card-title">🛒 下单面板
      <span class="sub">常驻面板 · 绑定当前体系：{{ acct.tier_name || '基础' }}（与行情页一致）</span></div>
    <trade-order-box ref="orderBox" :active="active" title="模拟交易下单"></trade-order-box>
  </div>

  <div class="lw-split">
    <div class="lw-card">
      <div class="lw-card-title">📋 当日委托 <span class="sub">{{ orders.length }} 笔</span></div>
      <div v-if="!orders.length" class="lw-empty">暂无委托</div>
      <table v-else class="rv-table">
        <thead><tr><th>时间</th><th>代码</th><th>方向</th><th>类型</th><th>委托价</th><th>数量</th><th>状态</th><th></th></tr></thead>
        <tbody>
          <tr v-for="o in orders" :key="o.order_id">
            <td>{{ (o.created_at || '').slice(11, 19) }}</td>
            <td>{{ o.code }}</td>
            <td :class="o.side === 'BUY' ? 'up' : 'down'">{{ o.side === 'BUY' ? '买入' : '卖出' }}</td>
            <td>{{ o.type === 'MARKET' ? '市价' : '限价' }}</td>
            <td>{{ o.price }}</td>
            <td>{{ o.quantity }}</td>
            <td>{{ statusLabel(o.status) }}</td>
            <td>
              <button v-if="o.status === 'PENDING'" class="btn tiny danger"
                      @click="cancelOrder(o.order_id)">撤单</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="lw-card">
      <div class="lw-card-title">🧾 当日成交 <span class="sub">{{ trades.length }} 笔</span></div>
      <div v-if="!trades.length" class="lw-empty">暂无成交</div>
      <table v-else class="rv-table">
        <thead><tr><th>时间</th><th>代码</th><th>方向</th><th>成交价</th><th>数量</th><th>金额</th><th>手续费</th></tr></thead>
        <tbody>
          <tr v-for="t in trades" :key="t.trade_id">
            <td>{{ (t.created_at || '').slice(11, 19) }}</td>
            <td>{{ t.code }}</td>
            <td :class="t.side === 'BUY' ? 'up' : 'down'">{{ t.side === 'BUY' ? '买入' : '卖出' }}</td>
            <td>{{ t.price }}</td>
            <td>{{ t.quantity }}</td>
            <td>{{ money(t.amount) }}</td>
            <td>{{ t.fee }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>

  <div v-if="msg" class="lw-msg">{{ msg }}</div>
</div>
`,
    setup(props) {
      const tiers = ref([]);
      const acct = ref({ tier: 'basic', positions: [] });
      const orders = ref([]);
      const trades = ref([]);
      const msg = ref('');
      const orderBox = ref(null);      // 下单面板实例 (点击持仓行时填充标的)

      // 点击持仓个股 → 自动填充至下单面板
      function fillPosition(p) {
        if (!p || !p.code) return;
        const box = orderBox.value;
        if (box && typeof box.fill === 'function') box.fill(p.code, p.name);
        msg.value = '已填充 ' + (p.name || '') + ' ' + p.code + ' 至下单面板';
      }

      function statusLabel(s) {
        return { PENDING: '挂单中', FILLED: '已成交', CANCELED: '已撤单', REJECTED: '已拒绝' }[s] || s;
      }

      async function loadAll() {
        try {
          const t = await api('/live/tiers');
          tiers.value = t.tiers || [];
        } catch (e) { console.error('档位加载失败', e.message); }
        try { acct.value = await api('/live/account'); }
        catch (e) { console.error('账户加载失败', e.message); }
        try { orders.value = (await api('/live/orders')).orders || []; }
        catch (e) { console.error('委托加载失败', e.message); }
        try { trades.value = (await api('/live/trades')).trades || []; }
        catch (e) { console.error('成交加载失败', e.message); }
      }

      async function bindTier(tier) {
        if (acct.value.tier === tier) return;
        msg.value = '';
        try {
          acct.value = await api('/live/tier/' + tier, { method: 'POST', body: '{}' });
          msg.value = '✅ 已切换资金体系（各体系账户独立，切换不重置）';
          await loadAll();
        } catch (e) { msg.value = '❌ ' + e.message; }
      }

      async function resetAccount() {
        if (!confirm('确定重置「当前体系」账户？仅清空当前体系的资金/持仓/委托/成交，其他体系与历史回合保留。')) return;
        msg.value = '';
        try {
          acct.value = await api('/live/reset', { method: 'POST', body: '{}' });
          msg.value = '✅ 已重置当前体系账户为初始资金';
          await loadAll();
        } catch (e) { msg.value = '❌ ' + e.message; }
      }

      async function cancelOrder(orderId) {
        try {
          await api('/live/orders/' + orderId, { method: 'DELETE' });
          await loadAll();
        } catch (e) { msg.value = '❌ ' + e.message; }
      }

      // 下单面板提交后刷新账户/委托/成交 (子组件无回传, 用轮询兜底)
      let timer = null;
      watch(() => props.active, function (v) {
        if (timer) { clearInterval(timer); timer = null; }
        if (v) { loadAll(); timer = setInterval(loadAll, 4000); }
      }, { immediate: true });
      onUnmounted(() => { if (timer) clearInterval(timer); });

      return { tiers, acct, orders, trades, msg, orderBox, fillPosition,
               loadAll, bindTier, resetAccount, cancelOrder, statusLabel, money, signClass };
    },
  };
})();

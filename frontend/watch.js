/**
 * 看盘(盘面) Tab —— 自有看盘界面 (数据源: 同花顺 iFinD)
 *
 * 后端 /api/watch/panorama 优先返回 iFinD 实时数据 (mode='live'); 密钥缺失或取数
 * 失败时逐板块回落占位快照 (mode='reserve', 带 errors 说明), 前端据此展示真实/占位标签。
 * 界面结构 (国内指数 / 日韩美股 / 涨跌家数 + A股总成交额 / 热点板块 /
 * 涨幅·跌幅·快速涨幅·成交额 / 涨停·跌停·炸板) 对两种模式通用。
 *
 * 需求101805: 指数分时缩略图 (0)、指数/板块点击跳行情 (1)、涨跌家数下方总成交额 (2)、
 * 个股四榜排序 (3)、板块按热点定位按涨幅展示 (4)。
 * 需求101807-0: 榜单「查看更多」—— 单卡片默认 12 行, 点击后经 /api/watch/list 取更长榜单弹层展示。
 * 需求101810: 个股四榜 (涨幅/跌幅/快速涨幅/成交额) 提升权重, 合并为分 tab 卡片置于热点板块之前(左侧);
 *             热点板块移至个股榜单 Tab 右侧展示。
 */
(function () {
  'use strict';
  const { ref, computed, onMounted, watch } = Vue;

  async function api(path, opts) {
    // 需求101602: 统一走预加载/缓存层 (看盘全景为慢接口, 走缓存 + 预加载), 见 preload.js
    return window.__apiFetch(path, opts);
  }

  function pctClass(v) { return v > 0 ? 'up' : v < 0 ? 'down' : ''; }
  function sign(v) { return (v > 0 ? '+' : '') + v; }
  function nb(v) { return v == null ? '--' : v; }   // iFinD 无平盘/炸板口径 -> 展示 --
  function fmtAmt(n) {
    if (n == null) return '--';
    const a = Math.abs(n);
    if (a >= 1e8) return (n / 1e8).toFixed(2) + '亿';
    if (a >= 1e4) return (n / 1e4).toFixed(2) + '万';
    return Number(n).toFixed(2);
  }

  // 需求101807-0: 「查看更多」弹层标题
  const MORE_TITLES = {
    hot_boards: '🔥 热点板块', gain_stocks: '📈 涨幅榜', loss_stocks: '📉 跌幅榜',
    fast_stocks: '⚡ 快速涨幅', amount_stocks: '💰 成交额榜',
    limit_up_today: '🚀 当日涨停股', limit_up_prev: '🕘 昨日涨停股',
    limit_down: '📉 跌停股', zhaban: '💥 炸板股',
  };

  // 需求101810: 个股四榜分 tab 定义 (mid 决定行中列展示口径: turn=换手 / speed=涨速 / amount=成交额)
  const RANK_TABS = [
    { key: 'gain_stocks', label: '📈 涨幅榜', note: '按涨幅排序', mid: 'turn' },
    { key: 'loss_stocks', label: '📉 跌幅榜', note: '按跌幅排序', mid: 'turn' },
    { key: 'fast_stocks', label: '⚡ 快速涨幅', note: '按5日涨速排序', mid: 'speed' },
    { key: 'amount_stocks', label: '💰 成交额榜', note: '按成交额排序', mid: 'amount' },
  ];

  window.WatchPane = {
    name: 'watch-pane',
    props: { active: { type: Boolean, default: false } },
    template: `
<div class="watch-wrap">

  <div class="watch-head">
    <span class="watch-title">📡 实时盘面 <span class="sub">聚合同花顺 · 开盘啦 · 自有盘面界面</span></span>
    <span class="watch-meta" v-if="data">更新 {{ (data.updated_at || '').slice(11, 16) }}</span>
    <button class="btn mini ghost" :disabled="loading" @click="load">{{ loading ? '⏳ 刷新中' : '🔄 刷新' }}</button>
  </div>

  <div class="watch-notice" v-if="data">
    <template v-if="data.mode === 'live'">
      📡 实时数据：数据来源「{{ (data.sources || []).join(' / ') }}」，实时行情由同花顺 iFinD 提供（level1 盘中数据，收盘后可能调整）。
      <span v-if="(data.errors || []).length">（部分板块回落占位：{{ data.errors.join('；') }}）</span>
    </template>
    <template v-else>
      🧩 暂未接入实时数据：数据源标注为「{{ (data.sources || []).join(' / ') }}」，当前展示占位快照。
      <span v-if="(data.errors || []).length">（{{ data.errors.join('；') }}）</span>
    </template>
  </div>

  <div v-if="error" class="err-box">{{ error }}</div>

  <template v-if="data">
    <!-- 国内指数 (需求101805-0: 指数分时缩略图) -->
    <div class="watch-block-title">国内指数</div>
    <div class="watch-indices">
      <div v-for="ix in data.indices" :key="ix.code" class="widx-card clickable"
           title="点击查看该指数K线/分时" @click="openIndex(ix)">
        <div class="widx-name">{{ ix.name }} <span class="widx-code">{{ ix.code }}</span></div>
        <div class="widx-price" :class="pctClass(ix.pct)">{{ ix.price }}</div>
        <div class="widx-pct" :class="pctClass(ix.pct)">{{ sign(ix.pct) }}% · {{ fmtAmt(ix.amount) }}</div>
        <svg v-if="idxSparkPath(ix.code)" class="widx-spark" :class="idxSparkCls(ix.code)"
             viewBox="0 0 60 20" preserveAspectRatio="none">
          <polyline :points="idxSparkPath(ix.code)"></polyline>
        </svg>
      </div>
    </div>

    <!-- 日韩美股 -->
    <div class="watch-block-title">日韩美股</div>
    <div class="watch-foreign">
      <div v-for="fx in data.foreign" :key="fx.code" class="widx-card clickable"
           title="点击查看该指数K线/分时" @click="openIndex(fx)">
        <div class="widx-name">{{ fx.name }}</div>
        <div class="widx-price" :class="pctClass(fx.pct)">{{ fx.price }}</div>
        <div class="widx-pct" :class="pctClass(fx.pct)">{{ sign(fx.pct) }}%</div>
      </div>
    </div>

    <!-- 涨跌家数 (需求101805-2: 下方展示当日A股总成交额) -->
    <div class="watch-breadth">
      <div class="wb-cell"><span>上涨</span><b class="up">{{ data.breadth.up }}</b></div>
      <div class="wb-cell"><span>下跌</span><b class="down">{{ data.breadth.down }}</b></div>
      <div class="wb-cell"><span>平盘</span><b>{{ nb(data.breadth.flat) }}</b></div>
      <div class="wb-cell"><span>涨停</span><b class="up">{{ data.breadth.limit_up }}</b></div>
      <div class="wb-cell"><span>跌停</span><b class="down">{{ data.breadth.limit_down }}</b></div>
      <div class="wb-cell"><span>炸板</span><b class="warn">{{ nb(data.breadth.zhaban) }}</b></div>
      <div class="wb-cell"><span>停牌</span><b>{{ data.breadth.suspended }}</b></div>
    </div>
    <div class="watch-amount" v-if="data.market_amount != null">
      <span>当日A股总成交额</span><b>{{ fmtAmt(data.market_amount) }}</b>
      <span class="wc-note">沪深北三市综合指数合计</span>
    </div>

    <!-- 需求101810: 个股四榜提升权重 —— 分 tab 前置展示(左侧), 热点板块移至个股 Tab 右侧 -->
    <div class="watch-cols rank-cols">
      <div class="watch-card rank-card">
        <div class="watch-card-title">
          {{ rankTabMeta.label }} <span class="wc-note">{{ rankTabMeta.note }}</span>
          <span class="wc-more" @click.stop="openMore(rankTab)">查看更多 ›</span>
        </div>
        <div class="rank-tabs">
          <span v-for="t in RANK_TABS" :key="t.key" :class="['rank-tab', {active: rankTab === t.key}]"
                @click="rankTab = t.key">{{ t.label }}</span>
        </div>
        <div v-for="s in rankRows" :key="rankTab + s.code" class="wr-row clickable"
             :title="'点击查看 ' + s.name + ' K线/分时'" @click="openStock(s)">
          <span class="wr-name">{{ s.name }}</span>
          <span class="wr-code">{{ s.code }}</span>
          <span class="wr-price">{{ s.price }}</span>
          <span v-if="rankTabMeta.mid === 'speed'" class="wr-turn">涨速 {{ sign(s.speed) }}%</span>
          <span v-else-if="rankTabMeta.mid === 'amount'" class="wr-turn">{{ fmtAmt(s.amount) }}</span>
          <span v-else class="wr-turn">换手 {{ s.turnover }}%</span>
          <span class="wr-pct" :class="pctClass(s.pct)">{{ sign(s.pct) }}%</span>
        </div>
        <div v-if="!rankRows.length" class="watch-empty">暂无数据</div>
      </div>

      <div class="watch-card">
        <div class="watch-card-title">🔥 热点板块 <span class="wc-note">按涨幅排序 · 点击查看板块行情</span>
          <span class="wc-more" @click.stop="openMore('hot_boards')">查看更多 ›</span></div>
        <div v-for="b in data.hot_boards" :key="b.name" class="wb-row clickable"
             title="点击查看该板块K线/分时" @click="openBoard(b)">
          <span class="wb-name">{{ b.name }}</span>
          <span class="wb-leader" @click.stop="openStockByCode(b.leader_code, b.leader)"
                :title="'点击查看 ' + b.leader">{{ b.leader }} <i :class="pctClass(b.leader_pct)">{{ sign(b.leader_pct) }}%</i></span>
          <span class="wb-net" :class="pctClass(b.net_in)">{{ fmtAmt(b.net_in) }}</span>
          <span class="wb-pct" :class="pctClass(b.pct)">{{ sign(b.pct) }}%</span>
        </div>
      </div>
    </div>

    <!-- 需求101805-3: 当日涨停股 + 昨日涨停股 -->
    <div class="watch-cols">
      <div class="watch-card">
        <div class="watch-card-title up">🚀 当日涨停股 <span class="wc-note">点击查看K线</span>
          <span class="wc-more" @click.stop="openMore('limit_up_today')">查看更多 ›</span></div>
        <div v-for="s in data.limit_up_today" :key="'lu' + s.code" class="wr-row clickable"
             title="点击查看该股K线/分时" @click="openStock(s)">
          <span class="wr-name">{{ s.name }}</span>
          <span class="wr-code">{{ s.code }}</span>
          <span class="wr-price">{{ s.price }}</span>
          <span class="wr-turn">换手 {{ s.turnover }}%</span>
          <span class="wr-pct up">{{ sign(s.pct) }}%</span>
        </div>
      </div>

      <div class="watch-card">
        <div class="watch-card-title">🕘 昨日涨停股 <span class="wc-note">点击查看K线</span>
          <span class="wc-more" @click.stop="openMore('limit_up_prev')">查看更多 ›</span></div>
        <div v-for="s in data.limit_up_prev" :key="'lp' + s.code" class="wr-row clickable"
             title="点击查看该股K线/分时" @click="openStock(s)">
          <span class="wr-name">{{ s.name }}</span>
          <span class="wr-code">{{ s.code }}</span>
          <span class="wr-price">{{ s.price }}</span>
          <span class="wr-turn">换手 {{ s.turnover }}%</span>
          <span class="wr-pct" :class="pctClass(s.pct)">{{ sign(s.pct) }}%</span>
        </div>
      </div>
    </div>

    <!-- 需求101805-3: 跌停股 + 炸板股 -->
    <div class="watch-cols">
      <div class="watch-card">
        <div class="watch-card-title down">📉 跌停股 <span class="wc-note">点击查看K线</span>
          <span class="wc-more" @click.stop="openMore('limit_down')">查看更多 ›</span></div>
        <div v-for="s in data.limit_down" :key="'ld' + s.code" class="wr-row clickable"
             title="点击查看该股K线/分时" @click="openStock(s)">
          <span class="wr-name">{{ s.name }}</span>
          <span class="wr-code">{{ s.code }}</span>
          <span class="wr-price">{{ s.price }}</span>
          <span class="wr-turn">换手 {{ s.turnover }}%</span>
          <span class="wr-pct down">{{ sign(s.pct) }}%</span>
        </div>
      </div>

      <div class="watch-card">
        <div class="watch-card-title warn">💥 炸板股 <span class="wc-note">点击查看K线</span>
          <span class="wc-more" @click.stop="openMore('zhaban')">查看更多 ›</span></div>
        <div v-for="s in data.zhaban" :key="'zb' + s.code" class="wr-row clickable"
             title="点击查看该股K线/分时" @click="openStock(s)">
          <span class="wr-name">{{ s.name }}</span>
          <span class="wr-code">{{ s.code }}</span>
          <span class="wr-price">{{ s.price }}</span>
          <span class="wr-turn">换手 {{ s.turnover }}%</span>
          <span class="wr-pct warn">{{ sign(s.pct) }}%</span>
        </div>
      </div>
    </div>
  </template>

  <div v-else-if="loading" class="watch-empty">⏳ 正在加载盘面数据...</div>

  <!-- 需求101807-0: 榜单「查看更多」弹层 -->
  <div v-if="more" class="watch-modal-mask" @click.self="closeMore">
    <div class="watch-modal">
      <div class="wm-head">
        <span class="wm-title">{{ more.title }}</span>
        <span class="wc-note" v-if="more.kind === 'board'">按涨幅排序 · 点击查看板块行情</span>
        <span class="wc-note" v-else>{{ more.rows.length }} 只 · 点击查看K线</span>
        <button class="btn mini ghost" @click="closeMore">关闭 ✕</button>
      </div>
      <div class="wm-body">
        <div v-if="more.loading" class="watch-empty">⏳ 加载中...</div>
        <template v-else>
          <div v-if="more.mode === 'reserve'" class="wm-warn">实时取数失败, 展示最近一次快照 ({{ more.error }})</div>
          <div v-for="(b, i) in more.rows" :key="'m' + (b.code || b.name) + i"
               class="wm-row clickable" @click="morePick(b)">
            <span class="wm-idx">{{ i + 1 }}</span>
            <span class="wm-name">{{ b.name }}</span>
            <span class="wm-code" v-if="b.code">{{ b.code }}</span>
            <span class="wm-sub" v-if="more.kind === 'board'">领涨 {{ b.leader }}
              <i :class="pctClass(b.leader_pct)">{{ sign(b.leader_pct) }}%</i></span>
            <span class="wm-sub" v-else-if="more.key === 'amount_stocks'">成交 {{ fmtAmt(b.amount) }}</span>
            <span class="wm-sub" v-else-if="more.key === 'fast_stocks'">涨速 {{ sign(b.speed) }}%</span>
            <span class="wm-sub" v-else>换手 {{ b.turnover }}%</span>
            <span class="wm-price" v-if="b.price != null">{{ b.price }}</span>
            <span class="wm-pct" :class="pctClass(b.pct)">{{ sign(b.pct) }}%</span>
          </div>
          <div v-if="!more.rows.length" class="watch-empty">暂无数据</div>
        </template>
      </div>
    </div>
  </div>
</div>
`,
    setup(props) {
      const data = ref(null);
      const loading = ref(false);
      const error = ref('');
      const idxSpark = ref({});        // 需求101805-0: 指数代码 -> [当日价格序列]
      // 需求101807-0: 榜单「查看更多」弹层状态 (null = 关闭)
      const more = ref(null);
      // 需求101810: 个股四榜分 tab (当前选中榜单 + 对应行数据/标题口径)
      const rankTab = ref('gain_stocks');
      const rankRows = computed(function () {
        return (data.value && data.value[rankTab.value]) || [];
      });
      const rankTabMeta = computed(function () {
        return RANK_TABS.find(function (t) { return t.key === rankTab.value; }) || RANK_TABS[0];
      });

      async function load() {
        if (loading.value) return;
        loading.value = true; error.value = '';
        try {
          data.value = await api('/watch/panorama');
          loadIdxSparks();
        }
        catch (e) { error.value = '❌ ' + e.message; }
        finally { loading.value = false; }
      }

      // 需求101805-0: 拉取指数条分时缩略图 (后端等距下采样至 ≤48 点)
      async function loadIdxSparks() {
        const codes = (data.value?.indices || []).map(x => x.code).filter(Boolean);
        if (!codes.length) { idxSpark.value = {}; return; }
        try {
          const r = await api('/watch/index-sparklines?codes=' + codes.join(','));
          idxSpark.value = (r && r.items) || {};
        } catch (e) { console.warn('指数缩略图加载失败:', e.message); }
      }
      /** 价格序列 → SVG polyline 坐标串 (viewBox 0 0 60 20, min-max 归一化) */
      function idxSparkPath(code) {
        const arr = (idxSpark.value || {})[code];
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
      function idxSparkCls(code) {
        const arr = (idxSpark.value || {})[code];
        if (!arr || arr.length < 2) return '';
        return arr[arr.length - 1] >= arr[0] ? 'up' : 'down';
      }

      // 看盘数据取数较重 (iFinD 多次调用), 仅在切到本 Tab 时加载
      onMounted(function () { if (props.active) load(); });
      watch(function () { return props.active; }, function (v) { if (v && !data.value) load(); });

      // 需求101805-1: 点击指数/板块/个股 → 跳转「行情」模块展示对应 K线/分时
      // 板块跳转与自选分组对板块的处理一致 (经 __openChart.board → selectBoard)
      function openIndex(ix) { window.__openChart?.index(ix.code, ix.name, ix.price, ix.pct); }
      function openBoard(b) { if (b && b.name) window.__openChart?.board(b.name, 'industry'); }
      function openStock(s) { if (s && s.code) window.__openChart?.stock(s.code, s.name); }
      function openStockByCode(code, name) { if (code) window.__openChart?.stock(code, name); }

      // 需求101807-0: 「查看更多」—— 拉取更长榜单 (板块/个股), 失败回落当前全景短榜
      async function openMore(key) {
        const title = MORE_TITLES[key] || key;
        more.value = { key: key, title: title,
                       kind: key === 'hot_boards' ? 'board' : 'stock',
                       rows: [], mode: '', error: '', loading: true };
        try {
          const r = await api('/watch/list?key=' + encodeURIComponent(key) + '&limit=30');
          const rows = (r && r.items && r.items.length)
            ? r.items : (((data.value || {})[key]) || []);
          more.value.rows = rows;
          more.value.mode = (r && r.mode) || '';
          more.value.error = (r && r.error) || '';
        } catch (e) {
          more.value.rows = ((data.value || {})[key]) || [];
          more.value.mode = 'reserve';
          more.value.error = e.message;
        } finally { more.value.loading = false; }
      }
      function closeMore() { more.value = null; }
      /** 弹层行点击 → 跳转行情模块展示该标的, 并关闭弹层 */
      function morePick(b) {
        if (more.value && more.value.kind === 'board') openBoard(b); else openStock(b);
        closeMore();
      }

      return { data, loading, error, load, pctClass, sign, nb, fmtAmt,
               idxSparkPath, idxSparkCls,
               openIndex, openBoard, openStock, openStockByCode,
               more, openMore, closeMore, morePick,
               RANK_TABS, rankTab, rankRows, rankTabMeta };
    },
  };
})();

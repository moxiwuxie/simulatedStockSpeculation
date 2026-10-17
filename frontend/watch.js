/**
 * 看盘 Tab —— 自有看盘界面 (数据源: 同花顺 iFinD)
 *
 * 后端 /api/watch/panorama 优先返回 iFinD 实时数据 (mode='live'); 密钥缺失或取数
 * 失败时逐板块回落占位快照 (mode='reserve', 带 errors 说明), 前端据此展示真实/占位标签。
 * 界面结构 (国内指数 / 日韩美股 / 涨跌家数 / 异动板块 / 异动个股 / 涨停·跌停·炸板) 对两种模式通用。
 */
(function () {
  'use strict';
  const { ref, onMounted, watch } = Vue;

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

  window.WatchPane = {
    name: 'watch-pane',
    props: { active: { type: Boolean, default: false } },
    template: `
<div class="watch-wrap">

  <div class="watch-head">
    <span class="watch-title">📡 实时看盘 <span class="sub">聚合同花顺 · 开盘啦 · 自有看盘界面</span></span>
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
    <!-- 国内指数 -->
    <div class="watch-block-title">国内指数</div>
    <div class="watch-indices">
      <div v-for="ix in data.indices" :key="ix.code" class="widx-card clickable"
           title="点击查看该指数K线/分时" @click="openIndex(ix)">
        <div class="widx-name">{{ ix.name }} <span class="widx-code">{{ ix.code }}</span></div>
        <div class="widx-price" :class="pctClass(ix.pct)">{{ ix.price }}</div>
        <div class="widx-pct" :class="pctClass(ix.pct)">{{ sign(ix.pct) }}% · {{ fmtAmt(ix.amount) }}</div>
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

    <!-- 涨跌家数 -->
    <div class="watch-breadth">
      <div class="wb-cell"><span>上涨</span><b class="up">{{ data.breadth.up }}</b></div>
      <div class="wb-cell"><span>下跌</span><b class="down">{{ data.breadth.down }}</b></div>
      <div class="wb-cell"><span>平盘</span><b>{{ nb(data.breadth.flat) }}</b></div>
      <div class="wb-cell"><span>涨停</span><b class="up">{{ data.breadth.limit_up }}</b></div>
      <div class="wb-cell"><span>跌停</span><b class="down">{{ data.breadth.limit_down }}</b></div>
      <div class="wb-cell"><span>炸板</span><b class="warn">{{ nb(data.breadth.zhaban) }}</b></div>
      <div class="wb-cell"><span>停牌</span><b>{{ data.breadth.suspended }}</b></div>
    </div>

    <div class="watch-cols">
      <!-- 异动板块 -->
      <div class="watch-card">
        <div class="watch-card-title">🔥 异动板块</div>
        <div v-for="b in data.hot_boards" :key="b.name" class="wb-row clickable"
             title="点击查看该板块K线/分时" @click="openBoard(b)">
          <span class="wb-name">{{ b.name }}</span>
          <span class="wb-leader" @click.stop="openStockByCode(b.leader_code, b.leader)"
                :title="'点击查看 ' + b.leader">{{ b.leader }} <i :class="pctClass(b.leader_pct)">{{ sign(b.leader_pct) }}%</i></span>
          <span class="wb-net" :class="pctClass(b.net_in)">{{ fmtAmt(b.net_in) }}</span>
          <span class="wb-pct" :class="pctClass(b.pct)">{{ sign(b.pct) }}%</span>
        </div>
      </div>

      <!-- 异动个股 (涨幅排序) -->
      <div class="watch-card">
        <div class="watch-card-title up">📈 异动个股 <span class="wc-note">涨幅排序</span></div>
        <div v-for="s in data.hot_stocks" :key="'hs' + s.code" class="wr-row clickable"
             title="点击查看该股K线/分时" @click="openStock(s)">
          <span class="wr-name">{{ s.name }}</span>
          <span class="wr-code">{{ s.code }}</span>
          <span class="wr-price">{{ s.price }}</span>
          <span class="wr-turn">换手 {{ s.turnover }}%</span>
          <span class="wr-pct" :class="pctClass(s.pct)">{{ sign(s.pct) }}%</span>
        </div>
      </div>
    </div>

    <div class="watch-cols">
      <!-- 当日涨停股 -->
      <div class="watch-card">
        <div class="watch-card-title up">🚀 当日涨停股 <span class="wc-note">点击查看K线</span></div>
        <div v-for="s in data.limit_up_today" :key="'lu' + s.code" class="wr-row clickable"
             title="点击查看该股K线/分时" @click="openStock(s)">
          <span class="wr-name">{{ s.name }}</span>
          <span class="wr-code">{{ s.code }}</span>
          <span class="wr-price">{{ s.price }}</span>
          <span class="wr-turn">换手 {{ s.turnover }}%</span>
          <span class="wr-pct up">{{ sign(s.pct) }}%</span>
        </div>
      </div>

      <!-- 昨日涨停股 -->
      <div class="watch-card">
        <div class="watch-card-title">🕘 昨日涨停股 <span class="wc-note">点击查看K线</span></div>
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

    <div class="watch-cols">
      <!-- 跌停股 -->
      <div class="watch-card">
        <div class="watch-card-title down">📉 跌停股 <span class="wc-note">点击查看K线</span></div>
        <div v-for="s in data.limit_down" :key="'ld' + s.code" class="wr-row clickable"
             title="点击查看该股K线/分时" @click="openStock(s)">
          <span class="wr-name">{{ s.name }}</span>
          <span class="wr-code">{{ s.code }}</span>
          <span class="wr-price">{{ s.price }}</span>
          <span class="wr-turn">换手 {{ s.turnover }}%</span>
          <span class="wr-pct down">{{ sign(s.pct) }}%</span>
        </div>
      </div>

      <!-- 炸板股 -->
      <div class="watch-card">
        <div class="watch-card-title warn">💥 炸板股 <span class="wc-note">点击查看K线</span></div>
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

  <div v-else-if="loading" class="watch-empty">⏳ 正在加载看盘数据...</div>
</div>
`,
    setup(props) {
      const data = ref(null);
      const loading = ref(false);
      const error = ref('');

      async function load() {
        if (loading.value) return;
        loading.value = true; error.value = '';
        try { data.value = await api('/watch/panorama'); }
        catch (e) { error.value = '❌ ' + e.message; }
        finally { loading.value = false; }
      }

      // 看盘数据取数较重 (iFinD 多次调用), 仅在切到本 Tab 时加载
      onMounted(function () { if (props.active) load(); });
      watch(function () { return props.active; }, function (v) { if (v && !data.value) load(); });

      // 需求5: 点击指数/板块/个股 → 跳转「图表」模块展示对应 K线/分时
      function openIndex(ix) { window.__openChart?.index(ix.code, ix.name, ix.price, ix.pct); }
      function openBoard(b) { if (b && b.name) window.__openChart?.board(b.name); }
      function openStock(s) { if (s && s.code) window.__openChart?.stock(s.code, s.name); }
      function openStockByCode(code, name) { if (code) window.__openChart?.stock(code, name); }

      return { data, loading, error, load, pctClass, sign, nb, fmtAmt,
               openIndex, openBoard, openStock, openStockByCode };
    },
  };
})();

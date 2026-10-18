/**
 * 复盘 Tab —— 战法库 / 分战法统计 / 历史对局回看 / 复盘笔记 / 成长曲线
 *
 * 约束: ECharts 容器始终存在于 DOM (v-show, 不用 v-if)
 */
(function () {
  'use strict';
  const { ref, computed, nextTick, onMounted, watch } = Vue;

  async function api(path, opts) {
    // 需求101602: 统一走预加载/缓存层 (只读GET缓存 + 并发去重), 见 preload.js
    return window.__apiFetch(path, opts);
  }

  function waitForSize(id, timeout) {
    timeout = timeout || 3000;
    return new Promise(function (resolve) {
      const start = Date.now();
      (function tick() {
        const el = document.getElementById(id);
        if (el) {
          const r = el.getBoundingClientRect();
          if (r.width > 0 && r.height > 0) return resolve(el);
        }
        if (Date.now() - start > timeout) return resolve(document.getElementById(id));
        requestAnimationFrame(tick);
      })();
    });
  }

  /** 主题配色: ECharts canvas 不解析 var(), 需从 CSS 变量取值, 随黑/白主题联动 */
  function tc(name, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }

  // 需求3: 复盘历史对局渲染训练划线, 与训练模块同款风格 (同花顺红 + 点状虚线)
  const ANNO_RED = '#ff3b30';
  const ANNO_DASH = [3, 3];

  // Liang-Barsky 线段裁剪: 将线段裁剪到矩形内 (用于延长线/射线超出绘图区的部分)
  function clipLine(p1, p2, rect) {
    let x1 = p1[0], y1 = p1[1], x2 = p2[0], y2 = p2[1];
    const dx = x2 - x1, dy = y2 - y1;
    let t0 = 0, t1 = 1;
    const p = [-dx, dx, -dy, dy];
    const q = [x1 - rect.x, rect.x + rect.width - x1, y1 - rect.y, rect.y + rect.height - y1];
    for (let i = 0; i < 4; i++) {
      if (p[i] === 0) { if (q[i] < 0) return null; }
      else {
        const r = q[i] / p[i];
        if (p[i] < 0) { if (r > t1) return null; if (r > t0) t0 = r; }
        else { if (r < t0) return null; if (r < t1) t1 = r; }
      }
    }
    return [[x1 + t0 * dx, y1 + t0 * dy], [x1 + t1 * dx, y1 + t1 * dy]];
  }

  /** 训练划线 overlay 系列 (水平线/矩形/箭头/射线/直线), 随缩放平移实时重绘 */
  function buildAnnoSeries(annos, all) {
    return {
      name: '画线', type: 'custom', xAxisIndex: 0, yAxisIndex: 0,
      z: 30, silent: true, animation: false,
      // dataZoom filterMode='filter' 会过滤窗口外数据点, 故用全量索引驱动 renderItem
      data: all.map(function (b, i) { return [i, b.close]; }),
      renderItem: function (params, api2) {
        const grid = params.coordSys;
        if (!grid || !grid.width) return;
        const rect = { x: grid.x, y: grid.y, width: grid.width, height: grid.height };
        const children = [];
        const toPx = function (xi, yv) {
          const p = api2.coord([xi, yv]);
          if (!p || isNaN(p[0]) || isNaN(p[1])) return null;
          return p;
        };
        const pushLine = function (a2, b2, color, dash) {
          if (!a2 || !b2) return;
          const seg = clipLine(a2, b2, rect);
          if (!seg) return;
          const style = { stroke: color, lineWidth: 1.4 };
          if (dash) style.lineDash = dash;
          children.push({
            type: 'line',
            shape: { x1: seg[0][0], y1: seg[0][1], x2: seg[1][0], y2: seg[1][1] },
            style: style,
          });
        };
        annos.forEach(function (a) {
          if (a.type === 'hline') {
            const p = toPx(a.x, a.y);
            if (!p) return;
            children.push({
              type: 'line',
              shape: { x1: rect.x, y1: p[1], x2: rect.x + rect.width, y2: p[1] },
              style: { stroke: ANNO_RED, lineWidth: 1.4 },
            });
          } else if (a.type === 'rect') {
            const p1 = toPx(a.x1, a.y1), p2 = toPx(a.x2, a.y2);
            if (!p1 || !p2) return;
            const x = Math.max(rect.x, Math.min(p1[0], p2[0]));
            const y = Math.max(rect.y, Math.min(p1[1], p2[1]));
            const w = Math.min(rect.x + rect.width, Math.max(p1[0], p2[0])) - x;
            const h = Math.min(rect.y + rect.height, Math.max(p1[1], p2[1])) - y;
            if (w <= 0 || h <= 0) return;
            children.push({
              type: 'rect',
              shape: { x: x, y: y, width: w, height: h },
              style: { fill: 'transparent', stroke: ANNO_RED, lineWidth: 1.2, lineDash: [4, 3] },
            });
          } else if (a.type === 'arrow') {
            const p1 = toPx(a.x1, a.y1), p2 = toPx(a.x2, a.y2);
            if (!p1 || !p2) return;
            const dx = p2[0] - p1[0], dy = p2[1] - p1[1];
            const len = Math.sqrt(dx * dx + dy * dy);
            if (len < 2) return;
            const ux = dx / len, uy = dy / len;
            const N = 8;
            for (let k = 0; k < N; k++) {
              const t0 = k / N, t1 = (k + 1) / N;
              const wdt = 0.8 + (3.0 - 0.8) * t1;
              children.push({
                type: 'line',
                shape: { x1: p1[0] + ux * len * t0, y1: p1[1] + uy * len * t0,
                         x2: p1[0] + ux * len * t1, y2: p1[1] + uy * len * t1 },
                style: { stroke: ANNO_RED, lineWidth: wdt },
              });
            }
            const hw = 9, hl = 12;
            const bx = p2[0] - ux * hl, by = p2[1] - uy * hl;
            const px = -uy, py = ux;
            children.push({
              type: 'polygon',
              shape: { points: [[p2[0], p2[1]], [bx + px * hw, by + py * hw], [bx - px * hw, by - py * hw]] },
              style: { fill: ANNO_RED },
            });
          } else {
            const p1 = toPx(a.x1, a.y1), p2 = toPx(a.x2, a.y2);
            if (!p1 || !p2) return;
            const dx = p2[0] - p1[0], dy = p2[1] - p1[1];
            const len = Math.sqrt(dx * dx + dy * dy) || 1;
            const ux = dx / len, uy = dy / len;
            const FAR = 6000;
            if (a.type === 'ray') {
              pushLine(p1, [p1[0] + ux * FAR, p1[1] + uy * FAR], ANNO_RED, ANNO_DASH);
            } else {
              pushLine([p1[0] - ux * FAR, p1[1] - uy * FAR], [p1[0] + ux * FAR, p1[1] + uy * FAR], ANNO_RED, ANNO_DASH);
            }
          }
        });
        return { type: 'group', children: children };
      },
    };
  }

  window.ReviewPane = {
    name: 'review-pane',
    props: { active: { type: Boolean, default: false } },
    template: `
<div class="review-wrap">

  <!-- 双子 Tab: 训练 / 模拟交易 (数据展示格式一致) -->
  <div class="rv-subtabs">
    <span :class="['rv-subtab', {active: subTab === 'train'}]" @click="switchSub('train')">🎯 训练复盘</span>
    <span :class="['rv-subtab', {active: subTab === 'live'}]" @click="switchSub('live')">💹 模拟交易复盘</span>
  </div>

  <!-- 模拟交易: 资金体系分隔 (切换即切换全局当前体系, 各体系数据独立) -->
  <div class="rv-tierbar" v-if="subTab === 'live'">
    <span class="sub">资金体系</span>
    <span v-for="t in tiers" :key="t.key"
          :class="['chip', 'mini', {active: liveTier === t.key}]"
          @click="switchLiveTier(t.key)">
      {{ t.name }} ¥{{ (t.initial_cash / 10000) }}万
    </span>
    <span class="sub">（同一体系账户独立，切换不重置）</span>
  </div>

  <!-- 成长曲线 -->
  <div class="rv-card">
    <div class="rv-card-title">
      📈 个人成长曲线 <span class="sub">累计净值 (基准 100, 按对局先后按净值比例复利)</span>
      <span class="rv-right" v-if="subTab === 'train'">
        <span class="rv-reset-count">已重置 {{ equity.reset_count }} 次</span>
        <button class="btn mini ghost" :disabled="resetting" @click="resetEquity">♻ 重置收益曲线</button>
      </span>
    </div>
    <div v-if="subTab === 'train' && !equity.can_start" class="rv-warn">
      ⚠️ 收益曲线已归零 (-100%)，无法开启新训练；请先「重置收益曲线」再开始训练（训练记录仍保留）。
    </div>
    <div id="review-growth" class="rv-chart growth"></div>
  </div>

  <!-- 战法统计 -->
  <div class="rv-card" v-show="subTab === 'train'">
    <div class="rv-card-title">
      🧭 分战法统计
      <span class="rv-right">
        <input v-model="newStrategy" placeholder="新建战法..." @keyup.enter="addStrategy">
        <button class="btn mini" @click="addStrategy">＋ 新建</button>
      </span>
    </div>
    <div v-if="!stats.length" class="rv-empty">还没有战法，先新建一个再开始训练吧</div>
    <table v-else class="rv-table">
      <thead>
        <tr><th>战法</th><th>局数</th><th>胜率</th><th>盈亏比</th><th>总盈亏</th><th>平均收益</th><th>最大回撤</th><th>爆仓</th><th></th></tr>
      </thead>
      <tbody>
        <tr v-for="s in stats" :key="s.id === null ? 'none' : s.id"
            :class="{ active: selectedStrategy === s.id || (selectedStrategy === -1 && s.id === null) }"
            @click="selectStrategy(s)">
          <td><span class="dot" :style="{ background: s.color || '#8892b0' }"></span>{{ s.name }}</td>
          <td>{{ s.sessions }}</td>
          <td :class="s.win_rate >= 50 ? 'up' : 'down'">{{ s.win_rate }}%</td>
          <td>{{ s.pnl_ratio }}</td>
          <td :class="s.total_pnl > 0 ? 'up' : s.total_pnl < 0 ? 'down' : ''">{{ s.total_pnl }}</td>
          <td :class="s.avg_pnl_pct > 0 ? 'up' : s.avg_pnl_pct < 0 ? 'down' : ''">{{ s.avg_pnl_pct }}%</td>
          <td class="down">{{ s.max_drawdown }}%</td>
          <td :class="s.blowups ? 'down' : ''">{{ s.blowups }}</td>
          <td><button v-if="s.id !== null" class="btn tiny danger" @click.stop="delStrategy(s.id)">删除</button></td>
        </tr>
      </tbody>
    </table>
  </div>

  <!-- 历史对局 + 复盘详情 -->
  <div class="rv-split">
    <div class="rv-card list-card">
      <div class="rv-card-title">
        🗂 历史对局
        <span class="sub">{{ filteredHistory.length }} / {{ history.length }} 局</span>
        <span class="rv-right">
          <select v-model="selectedStrategy" class="mini-select">
            <option :value="undefined">全部</option>
            <option :value="-1">默认</option>
            <option v-for="s in strategies" :key="s.id" :value="s.id">{{ s.name }}</option>
          </select>
        </span>
      </div>
      <div v-if="!filteredHistory.length" class="rv-empty">
        {{ subTab === 'train' ? '没有符合条件的训练记录' : '没有符合条件的模拟交易记录' }}
      </div>
      <div v-else class="rv-list">
        <div v-for="h in filteredHistory" :key="h.session_id"
             :class="['rv-item', { active: current && current.session_id === h.session_id }]"
             @click="openDetail(h.session_id)">
          <div class="rv-item-1">
            <span class="tag">{{ h.category_label || h.category }}</span>
            <span v-if="subTab === 'live' && liveTierName" class="tag tier">{{ liveTierName }}</span>
            <span class="code">{{ h.code_name }} {{ h.code }}</span>
            <span class="date">{{ (h.created_at || '').slice(5, 16) }}</span>
            <span v-if="h.status === 'active'" class="tag doing">进行中</span>
            <span v-else-if="h.status === 'blown_up'" class="tag quit">已爆仓</span>
            <span v-else-if="h.status === 'abandoned'" class="tag quit">已放弃</span>
          </div>
          <div class="rv-item-2">
            <span :class="h.pnl > 0 ? 'up' : h.pnl < 0 ? 'down' : ''">{{ h.pnl }} ({{ h.pnl_pct }}%)</span>
            <span class="strategy">{{ h.strategy_name || '默认' }}</span>
          </div>
        </div>
      </div>
    </div>

    <div class="rv-card detail-card">
      <div class="rv-card-title">
        🔍 复盘详情
        <span class="sub" v-if="current">{{ current.code_name }} {{ current.code }} · {{ current.start_date }} ~ {{ current.end_date }}</span>
      </div>

      <div id="review-kline" class="rv-chart kline"></div>

      <div v-if="!current" class="rv-empty overlay">← 从左侧选择一局回看</div>

      <div v-if="current" class="detail-body">
        <div class="detail-grid">
          <div v-if="subTab === 'live'"><span>成交金额</span><b>{{ current.final_capital }}</b></div>
          <div v-else><span>最终资产</span><b>{{ current.final_capital }}</b></div>
          <div><span>盈亏</span><b :class="current.pnl > 0 ? 'up' : 'down'">{{ current.pnl }} ({{ current.pnl_pct }}%)</b></div>
          <div><span>胜率</span><b>{{ current.win_rate }}%</b></div>
          <div><span>交易次数</span><b>{{ current.trades_count }}</b></div>
          <div v-if="subTab === 'live'"><span>持仓天数</span><b>{{ current.hold_days }} 天</b></div>
          <div v-if="subTab === 'live'"><span>开仓价</span><b>{{ current.open_price }}</b></div>
          <div v-if="subTab === 'live'"><span>平仓价</span><b>{{ current.close_price }}</b></div>
          <div><span>最大回撤</span><b class="down">{{ current.max_drawdown }}%</b></div>
          <div><span>杠杆</span><b>{{ current.leverage }}x</b></div>
        </div>
        <div v-if="subTab === 'train' && !current.actions_count" class="rv-empty">
          本局没有买卖操作，仅展示K线走势（未操作的对局也会完整保留K线记录）
        </div>
        <div class="detail-edit">
          <select v-model="editStrategy">
            <option :value="null">默认</option>
            <option v-for="s in strategies" :key="s.id" :value="s.id">{{ s.name }}</option>
          </select>
          <button class="btn mini" @click="saveStrategy">保存归类</button>
          <input class="note-input" v-model="editNote" placeholder="复盘笔记: 记录本局思路与得失...">
          <button class="btn mini" @click="saveNote">保存笔记</button>
        </div>
        <div v-if="msg" class="saved-msg">{{ msg }}</div>
      </div>
    </div>
  </div>

  <!-- 模拟交易: 交割单 (对照交割单形式, 每笔成交独立成行: 买一笔 / 卖一笔) -->
  <div class="rv-card" v-show="subTab === 'live'">
    <div class="rv-card-title">
      📜 交割单
      <span class="sub">
        {{ deliveries.length }} 笔成交 · 买入 {{ delSummary.buy_count || 0 }} 笔 / 卖出 {{ delSummary.sell_count || 0 }} 笔
      </span>
    </div>
    <div v-if="!deliveries.length" class="rv-empty">
      还没有成交记录 — 去「模拟交易」下单后, 这里会逐笔生成交割单 (买入一笔、卖出一笔)
    </div>
    <div v-else class="del-body">
      <div class="del-sum">
        <span>买入金额 <b>{{ money(delSummary.buy_amount) }}</b></span>
        <span>卖出金额 <b>{{ money(delSummary.sell_amount) }}</b></span>
        <span>手续费合计 <b>{{ money(delSummary.fee_total) }}</b></span>
      </div>
      <table class="rv-table">
        <thead>
          <tr>
            <th>成交时间</th><th>代码</th><th>名称</th><th>买卖</th>
            <th>成交价</th><th>数量</th><th>成交金额</th><th>手续费</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="d in deliveries" :key="d.trade_id">
            <td>{{ (d.created_at || '').slice(0, 16) }}</td>
            <td>{{ d.code }}</td>
            <td>{{ d.name }}</td>
            <td :class="d.side === 'BUY' ? 'up' : 'down'">{{ d.side_label }}</td>
            <td>{{ d.price }}</td>
            <td>{{ d.quantity }}</td>
            <td>{{ money(d.amount) }}</td>
            <td>{{ d.fee }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- 模式归因 -->
  <div class="rv-card attr-card" v-show="subTab === 'train'">
    <div class="rv-card-title">
      🧬 模式归因 <span class="sub">清洗筛选训练记录 · 输出完整交易模式</span>
      <span class="rv-right attr-ctrl">
        <select v-model="attrStrategy" class="mini-select">
          <option :value="undefined">全部战法</option>
          <option :value="-1">默认</option>
          <option v-for="s in strategies" :key="s.id" :value="s.id">{{ s.name }}</option>
        </select>
        <select v-model="attrCategory" class="mini-select">
          <option value="stock">股票双盲</option>
          <option value="demon">妖股训练</option>
        </select>
        <button class="btn mini" :disabled="attrLoading" @click="loadAttribution">
          {{ attrLoading ? '分析中...' : '生成归因' }}
        </button>
      </span>
    </div>

    <div v-if="!attrib" class="rv-empty">点击「生成归因」, 基于你的训练记录输出交易模式分析</div>
    <div v-else class="attr-body">
      <div class="attr-overview">
        <div class="attr-ov-cell"><span>样本局数</span><b>{{ attrib.overview.sessions }}</b></div>
        <div class="attr-ov-cell"><span>完整交易</span><b>{{ attrib.overview.trips }}</b></div>
        <div class="attr-ov-cell"><span>胜率</span><b :class="attrib.overview.win_rate >= 50 ? 'up' : 'down'">{{ attrib.overview.win_rate }}%</b></div>
        <div class="attr-ov-cell"><span>盈亏比</span><b>{{ attrib.overview.pnl_ratio }}</b></div>
        <div class="attr-ov-cell"><span>盈利因子</span><b>{{ attrib.overview.profit_factor }}</b></div>
        <div class="attr-ov-cell"><span>期望收益</span><b :class="attrib.overview.expectancy >= 0 ? 'up' : 'down'">{{ attrib.overview.expectancy }}</b></div>
        <div class="attr-ov-cell"><span>平均持仓</span><b>{{ attrib.overview.avg_hold }} 根</b></div>
        <div class="attr-ov-cell"><span>最大回撤</span><b class="down">{{ attrib.overview.max_drawdown }}%</b></div>
        <div class="attr-ov-cell"><span>爆仓率</span><b :class="attrib.overview.blowup_rate ? 'down' : ''">{{ attrib.overview.blowup_rate }}%</b></div>
      </div>

      <div class="attr-section attr-conclusion">
        <div class="attr-sec-title">🎯 模式结论 <span class="attr-profile">{{ attrib.conclusion.profile }}</span></div>
        <p class="attr-summary">{{ attrib.conclusion.summary }}</p>
        <div class="attr-lists">
          <div v-if="attrib.conclusion.strengths.length" class="attr-list good">
            <b>优势</b><ul><li v-for="(x,i) in attrib.conclusion.strengths" :key="i">{{ x }}</li></ul>
          </div>
          <div v-if="attrib.conclusion.weaknesses.length" class="attr-list bad">
            <b>短板</b><ul><li v-for="(x,i) in attrib.conclusion.weaknesses" :key="i">{{ x }}</li></ul>
          </div>
          <div v-if="attrib.conclusion.suggestions.length" class="attr-list tip">
            <b>改进建议</b><ul><li v-for="(x,i) in attrib.conclusion.suggestions" :key="i">{{ x }}</li></ul>
          </div>
        </div>
      </div>

      <div class="attr-section attr-reason">
        <div class="attr-sec-title">🧩 逻辑推演 <span class="sub">观察 → 依据 → 推断 → 行动</span></div>
        <div v-if="!attrib.reasoning || !attrib.reasoning.length" class="rv-empty">
          样本不足, 完成 5 局以上训练后再看推演 (当前主要是样本量问题, 不是方法问题)
        </div>
        <div v-else class="attr-reason-list">
          <div v-for="(r, i) in attrib.reasoning" :key="i"
               :class="['attr-reason-item', 'sev-' + r.severity]">
            <div class="attr-reason-head">
              <span class="attr-reason-topic">{{ r.topic }}</span>
              <span :class="['attr-reason-conf', r.confidence]">置信度 {{ confLabel(r.confidence) }}</span>
            </div>
            <ol class="attr-reason-flow">
              <li><b>观察</b><span>{{ r.observation }}</span></li>
              <li><b>依据</b><span>{{ r.evidence_text }}</span></li>
              <li><b>推断</b><span>{{ r.inference }}</span></li>
              <li><b>行动</b><span>{{ r.action }}</span></li>
            </ol>
          </div>
        </div>
      </div>

      <div class="attr-section">
        <div class="attr-sec-title">📐 因子分析 <span class="sub">不同条件下的胜率 / 平均收益</span></div>
        <div v-if="!attrib.factors.length" class="rv-empty">暂无足够交易样本</div>
        <div v-else class="attr-factors">
          <div v-for="f in attrib.factors" :key="f.name" class="attr-factor">
            <div class="attr-factor-name">{{ f.name }}</div>
            <table class="rv-table mini">
              <thead><tr><th>条件</th><th>笔数</th><th>胜率</th><th>平均收益</th><th>均持仓</th></tr></thead>
              <tbody>
                <tr v-for="b in f.buckets" :key="b.label">
                  <td>{{ b.label }}</td><td>{{ b.trades }}</td>
                  <td :class="b.win_rate >= 50 ? 'up' : 'down'">{{ b.win_rate }}%</td>
                  <td :class="b.avg_pnl > 0 ? 'up' : b.avg_pnl < 0 ? 'down' : ''">{{ b.avg_pnl }}</td>
                  <td>{{ b.avg_hold }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>

      <div class="attr-cols">
        <div class="attr-section">
          <div class="attr-sec-title">🧭 操作纪律</div>
          <div class="attr-disc">
            <div v-for="d in attrib.discipline" :key="d.name" class="attr-disc-item">
              <span class="attr-disc-name">{{ d.name }}</span>
              <b class="attr-disc-val">{{ d.value }}</b>
              <span class="attr-disc-comment">{{ d.comment }}</span>
            </div>
          </div>
        </div>

        <div class="attr-section">
          <div class="attr-sec-title">🎯 操作手法</div>
          <div class="attr-tac">
            <div class="attr-tac-row"><span>买入时机</span>
              <span v-for="t in attrib.tactics.buy_timing" :key="t.label" class="attr-chip">
                {{ t.label }} {{ t.trades }}笔 ({{ t.pct }}%)</span>
            </div>
            <div class="attr-tac-row"><span>卖出时机</span>
              <span v-for="t in attrib.tactics.sell_timing" :key="t.label" class="attr-chip">
                {{ t.label }} {{ t.trades }}笔 ({{ t.pct }}%)</span>
            </div>
          </div>
        </div>
      </div>

      <div class="attr-section">
        <div class="attr-sec-title">⚖️ 仓位控制 <span class="sub">重仓亏损率 {{ attrib.positioning.heavy_loss_rate }}%</span></div>
        <table class="rv-table">
          <thead><tr><th>仓位档</th><th>笔数</th><th>胜率</th><th>平均收益</th><th>总盈亏</th></tr></thead>
          <tbody>
            <tr v-for="b in attrib.positioning.buckets" :key="b.label">
              <td>{{ b.label }}</td><td>{{ b.trades }}</td>
              <td :class="b.win_rate >= 50 ? 'up' : 'down'">{{ b.win_rate }}%</td>
              <td :class="b.avg_pnl > 0 ? 'up' : b.avg_pnl < 0 ? 'down' : ''">{{ b.avg_pnl }}</td>
              <td :class="b.total_pnl > 0 ? 'up' : b.total_pnl < 0 ? 'down' : ''">{{ b.total_pnl }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</div>
`,
    setup(props) {
      const history = ref([]);
      const stats = ref([]);
      const strategies = ref([]);
      const current = ref(null);
      const selectedStrategy = ref(undefined);
      const newStrategy = ref('');
      const editStrategy = ref(null);
      const editNote = ref('');
      const msg = ref('');
      const attrib = ref(null);
      const attrLoading = ref(false);
      const attrStrategy = ref(undefined);
      const attrCategory = ref('stock');     // 需求101506-5: 删除「全部品类」, 默认股票双盲
      const subTab = ref('train');          // 'train' 训练复盘 | 'live' 模拟交易复盘
      const tiers = ref([]);                 // 资金体系档位 (模拟交易复盘用)
      const liveTier = ref('');              // 当前体系 key
      const liveTierName = computed(function () {
        const t = tiers.value.find(function (x) { return x.key === liveTier.value; });
        return t ? t.name : '';
      });

      // 交割单 (模拟交易复盘): 每一笔成交独立成行, 买一笔 / 卖一笔
      const deliveries = ref([]);
      const delSummary = ref({ total: 0, buy_count: 0, sell_count: 0,
                               buy_amount: 0, sell_amount: 0, fee_total: 0 });

      // 收益曲线 (账号级): 后端按「最近一次重置之后」复利计算, 归零(nav<=0)则禁止开启训练
      const equity = ref({ nav: 100, curve: [], can_start: true, reset_count: 0, last_reset_at: '' });
      const resetting = ref(false);

      function money(x) {
        const n = Number(x || 0);
        if (Math.abs(n) >= 1e8) return (n / 1e8).toFixed(2) + '亿';
        if (Math.abs(n) >= 1e4) return (n / 1e4).toFixed(2) + '万';
        return n.toFixed(2);
      }

      // 置信度中文化 (逻辑推演 chip)
      function confLabel(c) {
        return { high: '高', medium: '中', low: '低' }[c] || '低';
      }

      // 数据源按子 Tab 切换 (后端已把模拟交易回合归一化为训练同形字段)
      const histUrl = () => subTab.value === 'train' ? '/training/history' : '/live/history';
      const detailUrl = (id) => subTab.value === 'train'
        ? '/training/history/' + id : '/live/history/' + id;

      let growthChart = null;
      let klineChart = null;

      // 按战法筛选历史对局: undefined=全部, -1=未归类, 其余=对应战法 id
      const filteredHistory = computed(function () {
        const sel = selectedStrategy.value;
        if (sel === undefined) return history.value;
        return history.value.filter(function (h) {
          if (sel === -1) return !h.strategy_id;
          return h.strategy_id === sel;
        });
      });

      function selectStrategy(s) {
        selectedStrategy.value = (s.id === null || s.id === undefined) ? -1 : s.id;
      }

      async function loadAll() {
        try { history.value = (await api(histUrl() + '?limit=200')).sessions || []; }
        catch (e) { console.error('历史加载失败', e.message); }
        try { strategies.value = (await api('/strategies')).strategies || []; }
        catch (e) { console.error('战法加载失败', e.message); }
        if (subTab.value === 'train') {
          try { stats.value = (await api('/strategies/stats')).stats || []; }
          catch (e) { console.error('统计加载失败', e.message); }
          // 账号收益曲线 (重置后口径) + 归零拦截态
          try { equity.value = await api('/review/equity'); }
          catch (e) { console.error('收益曲线加载失败', e.message); }
        } else {
          stats.value = [];
          // 模拟交易: 拉取资金体系档位 + 当前体系 (与全局同步)
          try {
            if (!tiers.value.length) tiers.value = (await api('/live/tiers')).tiers || [];
            liveTier.value = (await api('/live/account')).tier || '';
          } catch (e) { console.error('资金体系加载失败', e.message); }
          // 交割单: 逐笔成交 (买一笔 / 卖一笔), 未平仓也能展示
          try {
            const d = await api('/live/deliveries?limit=200');
            deliveries.value = d.items || [];
            delSummary.value = d.summary || delSummary.value;
          } catch (e) { console.error('交割单加载失败', e.message); }
        }
        redraw();
      }

      /** 复盘内切换资金体系 → 同步全局当前体系, 重载该体系回合 */
      async function switchLiveTier(tier) {
        if (liveTier.value === tier) return;
        msg.value = '';
        try {
          const a = await api('/live/tier/' + tier, { method: 'POST', body: '{}' });
          liveTier.value = a.tier || tier;
          current.value = null;
          await loadAll();
        } catch (e) { msg.value = '❌ ' + e.message; }
      }

      /** 切换子 Tab: 清空当前详情并重载对应数据源 */
      function switchSub(t) {
        if (subTab.value === t) return;
        subTab.value = t;
        current.value = null;
        selectedStrategy.value = undefined;
        attrib.value = null;
        msg.value = '';
        loadAll();
      }

      /** 重置收益曲线起点 (保留全部训练记录, 从当前时刻重新起算) 并累加重置次数 */
      async function resetEquity() {
        if (resetting.value) return;
        const n = equity.value.reset_count || 0;
        if (!window.confirm('确认重置收益曲线？将把累计净值起点归回基准 100，'
          + '全部训练记录仍保留（当前已重置 ' + n + ' 次）。')) return;
        resetting.value = true;
        msg.value = '';
        try {
          equity.value = await api('/review/equity/reset', { method: 'POST', body: '{}' });
          msg.value = '✅ 收益曲线已重置（第 ' + equity.value.reset_count + ' 次）';
          await drawGrowth();
        } catch (e) { msg.value = '❌ ' + e.message; }
        finally { resetting.value = false; }
      }

      /** 只在 Tab 可见时初始化/刷新图表 —— 隐藏时容器尺寸为 0, ECharts 无法建图 */
      async function redraw() {
        if (!props.active) return;
        await nextTick();
        await drawGrowth();
        if (current.value) await drawDetail();
        else if (klineChart) { klineChart.dispose(); klineChart = null; }
      }

      async function addStrategy() {
        const name = (newStrategy.value || '').trim();
        if (!name) return;
        try {
          await api('/strategies', { method: 'POST', body: JSON.stringify({ name: name }) });
          newStrategy.value = '';
          await loadAll();
        } catch (e) { msg.value = '❌ ' + e.message; }
      }

      async function delStrategy(id) {
        try {
          await api('/strategies/' + id, { method: 'DELETE' });
          if (selectedStrategy.value === id) selectedStrategy.value = undefined;
          if (attrStrategy.value === id) attrStrategy.value = undefined;
          await loadAll();
        } catch (e) { msg.value = '❌ ' + e.message; }
      }

      // ---------- 模式归因 ----------
      async function loadAttribution() {
        attrLoading.value = true;
        try {
          const qs = ['limit=200'];
          const st = attrStrategy.value;
          if (st === -1 || typeof st === 'number') qs.push('strategy_id=' + st);
          if (attrCategory.value) qs.push('category=' + attrCategory.value);
          attrib.value = await api('/review/attribution?' + qs.join('&'));
          msg.value = '';
        } catch (e) { attrib.value = null; msg.value = '❌ ' + e.message; }
        finally { attrLoading.value = false; }
      }

      async function openDetail(sid) {
        try {
          const d = await api(detailUrl(sid));
          current.value = d;
          editStrategy.value = d.strategy_id || null;
          editNote.value = d.note || '';
          msg.value = '';
          await nextTick();
          drawDetail();
        } catch (e) { console.error('详情加载失败', e.message); }
      }

      async function saveStrategy() {
        if (!current.value) return;
        try {
          const d = await api(detailUrl(current.value.session_id), {
            method: 'PATCH', body: JSON.stringify({ strategy_id: editStrategy.value }),
          });
          current.value = d; msg.value = '✅ 归类已保存';
          await loadAll();
        } catch (e) { msg.value = '❌ ' + e.message; }
      }

      async function saveNote() {
        if (!current.value) return;
        try {
          const d = await api(detailUrl(current.value.session_id), {
            method: 'PATCH', body: JSON.stringify({ note: editNote.value }),
          });
          current.value = d; msg.value = '✅ 复盘笔记已保存';
          await loadAll();
        } catch (e) { msg.value = '❌ ' + e.message; }
      }

      // ---------- 成长曲线 ----------
      async function drawGrowth() {
        const el = await waitForSize('review-growth');
        if (!el) return;
        if (growthChart) { growthChart.dispose(); growthChart = null; }
        growthChart = echarts.init(el);
        // 训练复盘: 采用账号级收益曲线 (后端按「最近一次重置之后」复利加权, 基准 100)
        // 模拟交易复盘: 无重置概念, 由本地历史按净值比例复利累乘
        let cumData, dates, rows;
        if (subTab.value === 'train') {
          // 重置后仅有重置之后的结算局; 若尚无新局则为空曲线 (仅显示基准 100 虚线)
          rows = equity.value.curve || [];
          cumData = rows.map(function (r) { return Number(r.nav); });
          // 起点/重置锚点用「起点」「重置#N」作横轴标签; 对局用日期
          dates = rows.map(function (r) {
            return r.is_reset ? (r.label || '重置') : (r.created_at || '').slice(5, 10);
          });
        } else {
          rows = history.value.slice().reverse();   // 按时间正序
          // 累计净值: 按「最新净值比例」复利累乘 (而非简单相加百分比)
          // nav *= (1 + r), 展示为 基准100 的净值: 净值 200 时再亏 20% → 200*0.8=160
          let nav = 1.0;
          cumData = rows.map(function (r) {
            nav *= (1 + (Number(r.pnl_pct) || 0) / 100);
            if (nav < 0) nav = 0;
            return Number((nav * 100).toFixed(2));
          });
          dates = rows.map(function (r) { return (r.created_at || '').slice(5, 10); });
        }
        growthChart.setOption({
          backgroundColor: 'transparent',
          tooltip: {
            trigger: 'axis',
            formatter: function (ps) {
              const i = ps[0].dataIndex;
              const r = rows[i] || {};
              const nav = Number(ps[0].value);
              if (r.is_reset) {
                return '<b>' + (r.label || '重置') + '</b><br/>收益率归 0 (当前净值 100)';
              }
              return (r.created_at || '').slice(0, 16) + '<br/>累计净值 <b>' + nav.toFixed(2) +
                     '</b> (累计收益率 ' + (nav - 100).toFixed(2) + '%)<br/>本局 ' +
                     (r.pnl_pct > 0 ? '+' : '') + (r.pnl_pct || 0) + '% · ' +
                     ((r.code_name || '') + ' ' + (r.code || '')).trim();
            },
          },
          grid: { left: 50, right: 20, top: 20, bottom: 28 },
          xAxis: { type: 'category', data: dates,
                   axisLine: { lineStyle: { color: tc('--border-2', '#2a2f4a') } }, axisLabel: { color: tc('--text-dim', '#888'), fontSize: 10 } },
          yAxis: { type: 'value', scale: true, name: '净值', nameTextStyle: { color: tc('--text-dim', '#8892b0'), fontSize: 10 },
                   axisLine: { show: false },
                   splitLine: { lineStyle: { color: tc('--border', '#1e2347') } }, axisLabel: { color: tc('--text-dim', '#888'), fontSize: 10 } },
          series: [{
            name: '累计净值', type: 'line', data: cumData, smooth: true, showSymbol: true,
            symbolSize: 6, lineStyle: { color: tc('--brand', '#00b5ff'), width: 2 },
            itemStyle: { color: tc('--brand', '#00b5ff') },
            areaStyle: { color: tc('--brand-soft', 'rgba(0,181,255,0.12)') },
            markLine: { silent: true, symbol: 'none',
                        label: { show: true, formatter: '基准 100', color: tc('--text-dim', '#8892b0'), fontSize: 9, position: 'insideEndTop' },
                        lineStyle: { color: tc('--text-dim', '#8892b0'), type: 'dashed', width: 1 }, data: [{ yAxis: 100 }] },
          }],
        }, true);
        growthChart.resize();
      }

      // ---------- 单局 K 线复盘 (训练前置背景K线 + 训练K线, 完整保留) ----------
      async function drawDetail() {
        const d = current.value;
        const el = await waitForSize('review-kline');
        if (!el) return;
        if (klineChart) { klineChart.dispose(); klineChart = null; }
        klineChart = echarts.init(el);
        if (!d) return;
        const pre = d.pre_bars || [];
        const bars = d.bars || [];
        if (!bars.length) return;
        const preN = pre.length;
        // 前置背景K线 + 训练K线 连成一条序列, 完整展示训练前后的行情
        const all = pre.concat(bars);
        const x = all.map(function (b) { return b.date; });
        const ohlc = all.map(function (b) { return [b.open, b.close, b.low, b.high]; });
        const vols = all.map(function (b) { return b.volume; });
        const inTrain = function (i) { return i >= preN; };
        const mp = [];
        bars.forEach(function (b, i) {
          const di = preN + i;
          if (b.action === 'BUY') mp.push({ name: 'B', xAxis: di, yAxis: b.close, symbol: 'triangle', symbolSize: 13, itemStyle: { color: tc('--up', '#ff4757') }, label: { show: true, formatter: 'B', color: '#fff', fontSize: 9 } });
          else if (b.action === 'SELL') mp.push({ name: 'S', xAxis: di, yAxis: b.close, symbol: 'arrow', symbolRotate: 180, symbolSize: 13, itemStyle: { color: tc('--down', '#2ed573') }, label: { show: true, formatter: 'S', color: '#fff', fontSize: 9 } });
        });

        const series = [
          { name: 'K线', type: 'candlestick', data: ohlc,
            itemStyle: { color: tc('--up', '#ff4757'), color0: tc('--down', '#2ed573'),
                         borderColor: tc('--up', '#ff4757'), borderColor0: tc('--down', '#2ed573') } },
          { name: '成交量', type: 'bar', data: vols,
            // 前置段半透明, 训练段实色 (与训练模块一致)
            itemStyle: { color: function (p) {
              const i = p.dataIndex;
              const up = all[i].close >= all[i].open;
              if (!inTrain(i)) return up ? tc('--up-soft', 'rgba(255,71,87,0.5)') : tc('--down-soft', 'rgba(46,213,115,0.5)');
              return up ? tc('--up', '#ff4757') : tc('--down', '#2ed573');
            } },
            xAxisIndex: 1, yAxisIndex: 1 },
        ];
        if (mp.length) series[0].markPoint = { silent: true, data: mp };

        // 训练起点分隔线 + 训练段高亮
        if (preN > 0) {
          const zoneLabel = subTab.value === 'train' ? '训练开始 ▸' : '开仓 ▸';
          series[0].markLine = {
            silent: true, symbol: 'none',
            lineStyle: { color: '#e6a23c', type: 'dashed', width: 1 },
            label: { show: true, formatter: zoneLabel, color: '#e6a23c', fontSize: 10, position: 'start' },
            data: [{ xAxis: preN - 0.5 }],
          };
          series[0].markArea = {
            silent: true,
            data: [[{ xAxis: preN, itemStyle: { color: 'rgba(230,162,60,0.05)' } }, { xAxis: all.length - 1 }]],
          };
        }

        // 需求3: 同步渲染本局训练划线 (水平线/矩形/箭头/射线/直线)
        if (d.annos && d.annos.length) series.push(buildAnnoSeries(d.annos, all));

        klineChart.setOption({
          backgroundColor: 'transparent', animation: false,
          tooltip: {
            trigger: 'axis', axisPointer: { type: 'cross' },
            formatter: function (params) {
              const i = params[0].dataIndex; const k = all[i];
              if (!k) return '';
              const chg = i > 0 ? ((k.close - all[i - 1].close) / all[i - 1].close * 100).toFixed(2) : '--';
              const zone = inTrain(i) ? (subTab.value === 'train' ? '🎯 训练' : '💹 持仓') : '📊 背景';
              return zone + ' · <b>' + k.date + '</b><br/>开 ' + k.open + ' 收 ' + k.close + ' (' + chg + '%)<br/>高 ' + k.high + ' 低 ' + k.low;
            },
          },
          grid: [
            { left: 52, right: 16, top: 16, height: '54%' },
            { left: 52, right: 16, top: '72%', height: '13%' },
          ],
          xAxis: [
            { type: 'category', data: x, gridIndex: 0, axisLine: { lineStyle: { color: tc('--border-2', '#2a2f4a') } }, axisLabel: { color: tc('--text-dim', '#888'), fontSize: 10 } },
            { type: 'category', data: x, gridIndex: 1, axisLine: { show: false }, axisLabel: { show: false } },
          ],
          yAxis: [
            { scale: true, gridIndex: 0, axisLine: { show: false }, splitLine: { lineStyle: { color: tc('--border', '#1e2347') } }, axisLabel: { color: tc('--text-dim', '#888'), fontSize: 10 } },
            { scale: true, gridIndex: 1, axisLine: { show: false }, splitLine: { show: false }, axisLabel: { show: false } },
          ],
          series: series,
          dataZoom: [
            // 需求101506-3: 支持触控板左右滑动平移; 禁用滚轮/捏合缩放改范围
            { type: 'inside', xAxisIndex: [0, 1], start: 0, end: 100, moveOnMouseWheel: true, zoomOnMouseWheel: false, moveOnMouseMove: true, zoomOnMouseMove: false },
            { type: 'slider', xAxisIndex: [0, 1], start: 0, end: 100,
              bottom: 6, height: 30,
              borderColor: tc('--border-2', '#262b4d'), backgroundColor: tc('--slider-bg', 'rgba(38,42,77,0.4)'),
              fillerColor: tc('--slider-fill', 'rgba(38,167,255,0.15)'), handleStyle: { color: tc('--brand-2', '#26a7ff') },
              textStyle: { color: tc('--text-dim', '#8892b0'), fontSize: 9 } },
          ],
        }, true);
        klineChart.resize();
      }

      onMounted(function () {
        loadAll();
        window.addEventListener('resize', function () {
          if (growthChart) growthChart.resize();
          if (klineChart) klineChart.resize();
        });
        // 黑白主题切换 → 按新配色重绘收益曲线与复盘K线
        window.addEventListener('themechange', function () { redraw(); });
        // 需求101506-4: ↑/↓ 键缩放滑动条范围 (复盘页激活时生效, 输入框内不拦截)
        window.addEventListener('keydown', function (e) {
          if (!props.active || !klineChart) return;
          const el = e.target;
          const tag = ((el && el.tagName) || '').toUpperCase();
          if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || (el && el.isContentEditable)) return;
          if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
            e.preventDefault();
            window.KZoom(klineChart, e.key === 'ArrowUp' ? 'in' : 'out');
          }
        });
        // 需求101506-3: 触控板捏合(ctrl+wheel)不改滑动条范围, 同时阻止页面缩放
        const kEl = document.getElementById('review-kline');
        if (kEl) kEl.addEventListener('wheel', function (e) { if (e.ctrlKey) e.preventDefault(); }, { passive: false });
      });

      // Tab 切到复盘时: 刷新数据 + 建图 (此时容器才有尺寸)
      watch(function () { return props.active; }, function (v) {
        if (v) loadAll();
      });

      return {
        history, stats, strategies, current, selectedStrategy, newStrategy,
        editStrategy, editNote, msg,
        attrib, attrLoading, attrStrategy, attrCategory, loadAttribution, confLabel,
        filteredHistory, selectStrategy, subTab, switchSub,
        tiers, liveTier, liveTierName, switchLiveTier,
        deliveries, delSummary, money,
        equity, resetting, resetEquity,
        addStrategy, delStrategy, openDetail, saveStrategy, saveNote,
      };
    },
  };
})();

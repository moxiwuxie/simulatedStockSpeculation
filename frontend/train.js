/**
 * 训练 Tab —— 双盲 K 线盘感训练
 *
 * 闭环: 选品类 → 开始 → 逐根推进 → 买入/卖出/观望 → 自动结算 → 揭晓复盘
 * 约束: 本组件模板中 ECharts 容器始终存在于 DOM (用 v-show 而非 v-if)
 */
(function () {
  'use strict';
  const { ref, reactive, computed, nextTick, onMounted, watch } = Vue;

  async function api(path, opts, cfg) {
    // 需求101602: 统一走预加载/缓存层 (只读GET缓存 + 并发去重), 见 preload.js
    return window.__apiFetch(path, opts, cfg);
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
  const tc = function (name, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  };

  const fmt = function (n) {
    if (n == null) return '--';
    const a = Math.abs(n);
    if (a >= 1e8) return (n / 1e8).toFixed(2) + '亿';
    if (a >= 1e4) return (n / 1e4).toFixed(2) + '万';
    return Number(n).toLocaleString();
  };

  // 划线统一风格: 同花顺红 + 点状虚线 (直线/射线为点状样式)
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

  window.TrainPane = {
    name: 'train-pane',
    props: { active: { type: Boolean, default: false } },
    template: `
<div class="train-wrap">

  <!-- ============ 配置面板 ============ -->
  <div class="train-setup" v-show="!session">
    <div class="setup-title">🎯 K 线训练 <span class="sub">零风险盘感训练 · 逐根推进 · 双盲决策</span></div>

    <div v-if="!loggedIn" class="login-guide">
      🔒 训练数据需绑定账号，请先前往「账户」页登录后再开始训练。
    </div>

    <div class="setup-grid">
      <div class="setup-block">
        <label>选择品类</label>
        <div class="cat-chips">
          <span v-for="c in categories" :key="c.key"
                :class="['chip', { active: form.category === c.key }]"
                @click="pickCategory(c)">{{ c.label }}</span>
        </div>
        <div class="cat-hint" v-if="form.category === 'demon'">
          妖股训练：标的从历史妖股池抽取（涨停次数 / 区间涨幅 / 振幅异常 综合判定）
        </div>
      </div>

      <div class="setup-block">
        <label>单局 K 线根数 <span class="hint">最多 {{ barsMax }} 根</span></label>
        <div class="slider-row">
          <input type="range" min="20" :max="barsMax" :step="barsStep" v-model.number="form.bars">
          <span class="val">{{ form.bars }} 根</span>
        </div>
      </div>

      <div class="setup-block two-col">
        <div>
          <label>初始资金 (元)</label>
          <input type="number" step="10000" min="10000" v-model.number="form.initial_capital">
        </div>
        <div>
          <label>杠杆倍数</label>
          <input type="number" step="1" min="1" max="100" v-model.number="form.leverage">
        </div>
      </div>

      <div class="setup-block">
        <label>归属战法 <span class="hint">战法只是训练记录的标签, 不影响记录本身</span></label>
        <div class="slider-row">
          <select v-model="form.strategy_id">
            <option :value="null">默认</option>
            <option v-for="s in strategies" :key="s.id" :value="s.id">{{ s.name }}</option>
          </select>
          <button class="btn mini danger" :disabled="!form.strategy_id"
                  @click="delStrategy(form.strategy_id)" title="删除战法(仅取消记录标签, 保留训练记录)">🗑</button>
          <input class="new-strategy" v-model="newStrategyName" placeholder="新建战法名..."
                 @keyup.enter="addStrategy">
          <button class="btn mini" @click="addStrategy">＋</button>
        </div>
        <div class="anno-tip">删除战法只会取消训练记录上的标签, 不会删除任何训练记录</div>
      </div>

      <div class="setup-block">
        <label>指定标的 <span class="hint">留空 = 随机抽取 (双盲) · 支持代码 / 名称 / 拼音</span></label>
        <div class="code-search">
          <input v-model="codeKw" placeholder="如 600519 / 贵州茅台 / gzmt" autocomplete="off"
                 @input="onCodeInput" @focus="onCodeFocus" @keydown.enter.prevent="pickCodeFirst"
                 @keydown.down.prevent="codeOpen = codeResults.length > 0"
                 @keydown.esc="closeCodeList">
          <button v-if="codeKw" class="code-clear" @click="clearCodeKw" title="清空标的">✕</button>
          <div v-if="codeOpen && codeResults.length" class="tb-suggest">
            <div v-for="r in codeResults" :key="r.code" class="tb-suggest-item" @click="pickCode(r)">
              <span class="s-name">{{ r.name }}</span>
              <span class="s-code">{{ r.code }}</span>
              <span class="s-pct" :class="r.change_pct > 0 ? 'up' : (r.change_pct < 0 ? 'down' : '')">{{ r.change_pct == null ? '--' : r.change_pct + '%' }}</span>
            </div>
          </div>
        </div>
        <div v-if="codeLabel" class="code-picked">已选标的：{{ codeLabel }}</div>
      </div>

      <div class="setup-block">
        <label>起始日期 <span class="hint">与标的同填 = 非双盲训练 (展示名称与日期)</span></label>
        <div class="date-field" ref="dateField" @click="toggleCalendar">
          <span class="df-icon">📅</span>
          <span class="df-text" :class="{ ph: !form.start_date }">{{ form.start_date || '选择起始交易日' }}</span>
          <span v-if="form.start_date" class="df-clear" @click.stop="clearDate" title="清空日期">✕</span>
        </div>
        <div v-if="availHint" class="avail-hint">{{ availHint }}</div>

        <!-- 日历弹层: position:fixed, 免被 .train-wrap 的 overflow 裁切 -->
        <div v-if="dateOpen" class="date-pop" :style="datePopStyle">
          <div class="dp-head">
            <button class="dp-nav" @click="stepYear(-1)" title="上一年">«</button>
            <button class="dp-nav" @click="stepMonth(-1)" title="上一月">‹</button>
            <span class="dp-title">{{ calY }} 年 {{ calM }} 月</span>
            <button class="dp-nav" @click="stepMonth(1)" title="下一月">›</button>
            <button class="dp-nav" @click="stepYear(1)" title="下一年">»</button>
            <button class="dp-close" @click="closeCalendar" title="关闭">✕</button>
          </div>
          <div class="dp-quick">
            <button v-for="q in quickRanges" :key="q.key" class="dp-qbtn" @click="quickPick(q)">{{ q.label }}</button>
            <button class="dp-qbtn ghost" @click="clearDate">清空</button>
          </div>
          <div class="dp-dow"><span v-for="d in dowLabels" :key="d">{{ d }}</span></div>
          <div class="dp-grid">
            <span v-for="c in calCells" :key="c.key"
                  :class="['dp-cell', { muted: !c.inMonth, today: c.isToday, sel: c.isSel, disabled: c.disabled }]"
                  @click="pickDate(c)">{{ c.day }}</span>
          </div>
          <div class="dp-foot"><span class="dp-tip">{{ calTip }}</span></div>
        </div>
      </div>

      <div class="setup-block actions">
        <button class="btn mini ghost" @click="resetForm">🔄 恢复默认参数</button>
        <button class="btn mini ghost" @click="clearCode">✕ 清空标的 (随机双盲)</button>
        <button class="btn mini ghost" @click="openPool">📋 训练标的池</button>
        <span class="help-q" data-tip="查看当前可训练的个股池（含妖股/涨停/量能异常标记），可直接选为指定标的">?</span>
      </div>
    </div>

    <button class="btn primary big" :disabled="loading || !loggedIn" @click="start">
      {{ loading ? '⏳ 正在准备行情...' : (loggedIn ? '🚀 准备开始训练' : '🔒 请先登录后再开始训练') }}
    </button>
    <div v-if="error" class="err-box">{{ error }}</div>

    <div class="hint-box">
      <b>训练规则</b>：逐根 K 线推进，每根做出【买入 / 卖出 / 观望】决策，单局最多 {{ barsMax }} 根，
      推进到末尾自动结算盈亏。<b>双盲模式</b>下隐藏标的名称与日期，训练结束自动揭示。
    </div>
  </div>

  <!-- 提示条: 位于配置/运行两态之外, 训练开始后仍可见 (如「本局按 N 根进行」) -->
  <div v-if="notice" class="notice-box">{{ notice }}</div>

  <!-- ============ 训练进行中 ============ -->
  <div class="train-run" v-show="session">
    <!-- 顶栏: 进度 + 账户 -->
    <div class="run-top">
      <div class="blind-head">
        <span class="blind-title">🎯 {{ session && session.category_label }}</span>
        <span class="tag" :class="session && session.status === 'active' ? 'doing' : 'quit'" v-if="session">{{ session.status_label }}</span>
        <span class="blind-code revealed" v-if="session && (session.code_revealed || session.non_blind)">{{ session.code_name }} {{ session.code }} · {{ session.start_date }} ~ {{ session.end_date }}</span>
        <span class="blind-code" v-else-if="session && session.code">🎯 {{ session.code_name }} {{ session.code }}</span>
        <span class="blind-code" v-else-if="session">🔒 标的信息已隐藏</span>
      </div>
      <div class="progress-wrap">
        <div class="progress-bar"><div class="progress-fill" :style="{ width: progressPct + '%' }"></div></div>
        <span class="progress-text">{{ session ? session.bars_used : 0 }} / {{ session ? session.bars_total : 0 }}</span>
      </div>
      <div class="period-group">
        <span class="period-label">周期</span>
        <button v-for="p in trainPeriods" :key="p.key" :class="['btn','mini',{active: !!session && session.period === p.key}]"
                @click="switchPeriod(p.key)">{{ p.label }}</button>
      </div>
      <div class="lev-badge" v-if="session && session.leverage > 1">{{ session.leverage }}x 杠杆</div>
    </div>

    <!-- 账户面板 -->
    <div class="acct-strip" v-if="session">
      <div class="acct-cell"><span>总资产</span><b>{{ fmt(session.portfolio_value) }}</b></div>
      <div class="acct-cell"><span>可用现金</span><b>{{ fmt(session.cash) }}</b></div>
      <div class="acct-cell"><span>持仓</span><b :class="session.pos > 0 ? 'up' : session.pos < 0 ? 'down' : ''">{{ session.pos }}</b></div>
      <div class="acct-cell" v-if="session.pos > 0 && session.locked_qty > 0"><span>可卖(T+1)</span><b :class="session.sellable_qty > 0 ? 'up' : 'down'">{{ session.sellable_qty }}</b></div>
      <div class="acct-cell"><span>持仓均价</span><b>{{ session.avg_cost }}</b></div>
      <div class="acct-cell" v-if="session.margin > 0"><span>保证金</span><b>{{ fmt(session.margin) }}</b></div>
      <div class="acct-cell"><span>现价</span><b>{{ session.current_price }}</b></div>
      <div class="acct-cell"><span>浮动盈亏</span><b :class="pnlClass">{{ fmt(session.pnl) }}</b></div>
      <div class="acct-cell"><span>收益率</span><b :class="pnlClass">{{ session.pnl_pct }}%</b></div>
      <div class="acct-cell"><span>最大回撤</span><b class="down">{{ session.max_drawdown }}%</b></div>
    </div>

    <!-- 主视图: 左 = K线/成交量/MACD 三面板, 右 = 分时(同花顺风格) -->
    <div class="train-main">
      <div class="train-left">
        <div id="train-kline" class="chart-box train-chart"></div>
        <div class="train-toolbar">
          <div class="ma-bar">
            <span class="ma-label">均线</span>
            <label v-for="k in maKeys" :key="k" class="ma-item">
              <input type="checkbox" :value="k" v-model="maOn"> MA{{ k }}
            </label>
            <button class="btn mini ghost" @click="resetMa">恢复默认</button>
          </div>
          <div class="anno-bar">
            <span class="anno-label">画线</span>
            <button :class="['btn','mini',{active: drawMode==='select'}]" @click="setDrawMode('select')">选择</button>
            <button :class="['btn','mini',{active: drawMode==='hline'}]" @click="setDrawMode('hline')">水平线</button>
            <button :class="['btn','mini',{active: drawMode==='line'}]" @click="setDrawMode('line')">直线</button>
            <button :class="['btn','mini',{active: drawMode==='ray'}]" @click="setDrawMode('ray')">射线</button>
            <button :class="['btn','mini',{active: drawMode==='rect'}]" @click="setDrawMode('rect')">矩形</button>
            <button :class="['btn','mini',{active: drawMode==='arrow'}]" @click="setDrawMode('arrow')">箭头</button>
            <span class="anno-label">次数</span>
            <select class="anno-count-select" v-model.number="drawCount">
              <option :value="1">1 次</option>
              <option :value="2">2 次</option>
              <option :value="3">3 次</option>
              <option :value="5">5 次</option>
              <option :value="0">无限</option>
            </select>
            <span class="anno-tip" v-if="drawDone">✅ 划线完成</span>
            <span class="anno-tip" v-else-if="annoAnchor">已定起点, 再点一次确定终点</span>
            <span class="anno-count" v-else-if="drawCount">剩余 {{ annoLeft }} 次</span>
            <button class="btn mini ghost" v-if="drawDone" @click="continueDraw">继续划线</button>
            <span class="anno-count">已画 {{ annos.length }} 条</span>
            <button class="btn mini ghost" @click="clearAnnos">🗑 清空画线</button>
          </div>
        </div>
        <div class="ind-tip" v-if="session && !session.code_revealed">横轴为序号(隐藏日期) · 图内可左右滑动查看历史</div>
      </div>
      <div class="train-right">
        <div class="pre-head">
          <span class="pre-title">📊 分时 <span class="sub">同花顺风格 · 点击主图任意K线联动该日分时</span></span>
        </div>
        <div class="pane-sep">
          <span class="pane-date">{{ preDate ? preDate : '⬇ 点击主图K线查看分时' }}</span>
          <span class="pane-amp" v-if="preAmp != null"><b>振幅 {{ preAmp }}%</b></span>
        </div>
        <div id="train-intraday" class="chart-box small train-intraday"></div>
      </div>
    </div>

    <!-- 操作区 -->
    <div class="act-bar" v-if="session && session.status === 'active'">
      <div class="ratio-group">
        <span class="ratio-label">仓位</span>
        <select v-model.number="positionRatio" class="ratio-select">
          <option :value="1">满仓 (100%)</option>
          <option :value="0.5">二分之一仓 (50%)</option>
          <option :value="0.333">三分之一仓 (33%)</option>
          <option :value="0.2">五分之一仓 (20%)</option>
          <option :value="0.1">十分之一仓 (10%)</option>
        </select>
      </div>
      <input class="qty-input" type="number" min="1" v-model.number="qty" placeholder="数量(留空=按仓位)">
      <button class="btn buy" :disabled="!canBuy" :title="canBuy ? '' : '可用资金不足 1 手, 无法买入'" @click="act('buy')">📈 买入</button>
      <button class="btn sell" :disabled="sellLocked" :title="sellLocked ? '当日买入(T+1), 次日开盘后才能卖出' : ''" @click="act('sell')">📉 卖出</button>
      <button class="btn hold" @click="holdAndAdvance" title="观望 (快捷键 →)">观望</button>
      <button class="btn ghost" @click="advance5">观望 5 个交易日</button>
      <button class="btn ghost" @click="settle">💰 结算</button>
    </div>

    <!-- 需求4: 买入挂单提示 (次日开盘成交, 缩量一字板将无法买入) -->
    <div class="pending-tip" v-if="session && session.pending_buy">
      ⏳ 买入委托已挂出, 将于<b>次日开盘价</b>成交 (若遇缩量一字板则将无法买入)
    </div>

    <!-- 条件单: 止盈 / 止损 / 移动止损 (触发即操作) -->
    <div class="cond-bar" v-if="session && session.status === 'active'">
      <span class="cond-label">条件单</span>
      <select v-model="condKind" class="cond-select">
        <option value="take_profit">止盈</option>
        <option value="stop_loss">止损</option>
        <option value="trailing_stop">移动止损</option>
      </select>
      <input v-if="condKind !== 'trailing_stop'" class="cond-input" type="number" step="0.01" min="0"
             v-model.number="condPrice" placeholder="触发价">
      <template v-else>
        <input class="cond-input" type="number" step="0.5" min="0.5" max="100"
               v-model.number="condTrail" placeholder="回撤幅度">
        <span class="cond-tip">% 自最新价回撤</span>
      </template>
      <select v-model.number="condRatio" class="cond-select">
        <option :value="1">全部仓位</option>
        <option :value="0.5">一半仓位</option>
        <option :value="0.333">三分之一</option>
        <option :value="0.2">五分之一</option>
      </select>
      <button class="btn mini" @click="setConditional">＋ 挂条件单</button>
      <span class="cond-tip">触发价按 K 线高低价判定, 触及即尝试操作 (无可操作持仓则提示)</span>
      <span class="cond-tip warn" v-if="condNotice">⚠️ {{ condNotice }}</span>
      <div class="cond-list" v-if="activeConds.length">
        <div v-for="c in activeConds" :key="c.id" class="cond-item" :class="c.kind">
          <span>🎯 {{ c.kind_label }} @{{ c.trigger_price }}<template v-if="c.trail_pct"> (回撤 {{ c.trail_pct }}%)</template> · 平 {{ Math.round(c.ratio * 100) }}%</span>
          <button class="cond-del" @click="cancelConditional(c.id)" title="撤销">✕</button>
        </div>
      </div>
      <div class="cond-fired" v-if="firedConds.length">
        <span v-for="c in firedConds" :key="c.id" class="cond-fired-item" :class="c.kind">
          ✅ {{ c.kind_label }}触发 @{{ c.triggered_price }} · 盈亏 {{ c.triggered_pnl }}
        </span>
      </div>
      <div class="cond-fired" v-if="skippedConds.length">
        <span v-for="c in skippedConds" :key="c.id" class="cond-fired-item skipped">
          ⚠️ {{ c.kind_label }}触发 @{{ c.triggered_price }} · 当前无可操作持仓, 已跳过
        </span>
      </div>
    </div>

    <!-- 揭晓 / 复盘 -->
    <div class="reveal-box" v-if="session && session.code_revealed">
      <div class="reveal-title">🏁 训练结束 · 结果揭晓</div>
      <div class="reveal-grid">
        <div><span>标的</span><b>{{ session.code_name }} {{ session.code }}</b></div>
        <div><span>区间</span><b>{{ session.start_date }} ~ {{ session.end_date }}</b></div>
        <div><span>最终资产</span><b>{{ fmt(session.portfolio_value) }}</b></div>
        <div><span>总盈亏</span><b :class="pnlClass">{{ fmt(session.pnl) }} ({{ session.pnl_pct }}%)</b></div>
        <div><span>胜率</span><b>{{ session.win_rate }}%</b></div>
        <div><span>完整交易</span><b>{{ session.round_trips }} 笔</b></div>
        <div><span>最大回撤</span><b class="down">{{ session.max_drawdown }}%</b></div>
        <div><span>爆仓</span><b :class="session.blown_up ? 'down' : ''">{{ session.blown_up ? '是' : '否' }}</b></div>
      </div>
      <div class="reveal-actions">
        <div class="inline-field">
          <label>归类战法</label>
          <select v-model="form.strategy_id">
            <option :value="null">默认</option>
            <option v-for="s in strategies" :key="s.id" :value="s.id">{{ s.name }}</option>
          </select>
          <button class="btn mini" @click="saveStrategy">保存归类</button>
        </div>
        <div class="inline-field grow">
          <label>复盘笔记</label>
          <input v-model="note" placeholder="记录本局思路与得失...">
          <button class="btn mini" @click="saveNote">保存笔记</button>
        </div>
      </div>
      <div class="reveal-btns">
        <button class="btn primary" @click="restart">🔄 再来一局</button>
      </div>
      <div v-if="savedMsg" class="saved-msg">{{ savedMsg }}</div>
    </div>
  </div>

  <!-- ============ 训练标的池 (需求101802-1) ============ -->
  <div v-if="poolOpen" class="pool-mask" @click.self="closePool">
    <div class="pool-modal">
      <div class="pool-head">
        <span class="pool-title">📋 训练标的池 <span class="sub">当前可训练个股（未过期统计）</span></span>
        <button class="pool-close" @click="closePool" title="关闭">✕</button>
      </div>
      <div class="pool-filters">
        <span class="pool-sub">品类</span>
        <span v-for="c in categories" :key="c.key"
              :class="['pool-chip', { active: poolCategory === c.key }]"
              @click="pickPoolCategory(c.key)">{{ c.label }}</span>
      </div>
      <div class="pool-filters">
        <span class="pool-sub">筛选</span>
        <span v-for="f in poolFilters" :key="f.key"
              :class="['pool-chip', { active: poolFilter === f.key }]"
              @click="pickPoolFilter(f.key)">{{ f.label }}</span>
        <span class="pool-count">共 {{ poolTotal }} 只</span>
      </div>
      <div class="pool-body">
        <table class="pool-table">
          <thead>
            <tr>
              <th>标的</th><th>涨停</th><th>区间涨幅</th><th>最大振幅</th>
              <th>量能</th><th>妖股</th><th>分时</th><th>统计日</th><th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="r in poolRows" :key="r.code">
              <td class="pool-stock">
                <span class="p-name">{{ r.name || '—' }}</span>
                <span class="p-code">{{ r.code }}</span>
              </td>
              <td>{{ r.limit_up_count }} 次</td>
              <td :class="r.range_gain > 0 ? 'up' : (r.range_gain < 0 ? 'down' : '')">{{ poolPct(r.range_gain) }}</td>
              <td>{{ poolPct(r.max_amp) }}</td>
              <td><span v-if="r.vol_anomaly" class="pool-tag warn">异常</span><span v-else class="pool-dim">—</span></td>
              <td><span v-if="r.is_demon" class="pool-tag demon">妖股 {{ r.demon_score }}</span><span v-else class="pool-dim">—</span></td>
              <td><span v-if="r.has_intraday" class="pool-tag ok">有</span><span v-else class="pool-tag off">无</span></td>
              <td class="pool-dim">{{ r.stat_date || '—' }}</td>
              <td><button class="btn mini ghost" @click="usePoolRow(r)">选为标的</button></td>
            </tr>
            <tr v-if="!poolRows.length">
              <td colspan="9" class="pool-empty">
                {{ poolLoading ? '加载中...' : '池内暂无符合条件的标的（开启训练会自动写入池）' }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="pool-foot">
        <button v-if="poolHasMore" class="btn mini ghost" :disabled="poolLoading" @click="poolMore">
          {{ poolLoading ? '加载中...' : '加载更多' }}
        </button>
        <span class="pool-tip">点「选为标的」→ 关闭弹窗后自动填入指定标的输入框</span>
      </div>
    </div>
  </div>
</div>
`,
    setup(props) {
      const categories = ref([]);
      const strategies = ref([]);
      const session = ref(null);
      const loading = ref(false);
      const error = ref('');
      // 未登录 / 未绑手机号均不可开启训练 (训练数据绑定账号)
      const loggedIn = ref(false);
      function refreshLogin() {
        if (!localStorage.getItem('auth_token')) { loggedIn.value = false; return; }
        let u = null;
        try { u = JSON.parse(localStorage.getItem('auth_user') || 'null'); } catch (e) {}
        loggedIn.value = !!(u && u.phone);      // 手机号为空 = 需补绑, 视为不可用
      }
      refreshLogin();
      const newStrategyName = ref('');
      const note = ref('');
      const savedMsg = ref('');
      const qty = ref(null);
      const positionRatio = ref(1);   // 仓位比例: 满仓=1(默认), 二分之一=0.5 ...
      const condKind = ref('take_profit');   // 条件单类型: 止盈 / 止损 / 移动止损
      const condPrice = ref(null);           // 条件单触发价
      const condTrail = ref(null);           // 移动止损回撤幅度 (%)
      const condRatio = ref(1);              // 触发后平仓比例
      const condNotice = ref('');            // 条件单触发但不可操作时的提示
      const activeConds = computed(function () {
        return ((session.value && session.value.conditionals) || []).filter(function (c) { return c.status === 'active'; });
      });
      const firedConds = computed(function () {
        return ((session.value && session.value.conditionals) || []).filter(function (c) { return c.status === 'triggered'; });
      });
      const skippedConds = computed(function () {
        return ((session.value && session.value.conditionals) || []).filter(function (c) { return c.status === 'skipped'; });
      });
      const marks = ref([]);          // [{index, action, price}]
      const trainPeriods = [
        { key: 'day', label: '日线' }, { key: 'week', label: '周线' },
        { key: 'month', label: '月线' }, { key: 'year', label: '年线' },
      ];
      // MA 均线开关 (5/10/20/30/60/120/250)
      const maKeys = ['5', '10', '20', '30', '60', '120', '250'];
      const MA_COLORS = {
        '5': '#f0843f', '10': '#5b8ff9', '20': '#e8362f', '30': '#52c41a',
        '60': '#a259ff', '120': '#ff7a9e', '250': '#d4b106',
      };
      const maOn = ref(['5', '10', '20', '30', '60', '120', '250']);
      function resetMa() { maOn.value = ['5', '10', '20', '30', '60', '120', '250']; }
      // 画线功能 (同花顺式): 选择/水平线/直线/射线/矩形/箭头, drawMode 为 select 时点击仍联动分时
      const drawMode = ref('select');
      const annos = ref([]);          // [{type:'hline',x,y} | {type:'line'|'ray'|'arrow'|'rect',x1,y1,x2,y2}], x 为 dataIndex
      const annoAnchor = ref(null);   // 直线/矩形/箭头 起点 {x, y}
      const drawCount = ref(1);       // 本次划线允许的笔数 (0 = 无限), 默认 1 次
      const annoLeft = ref(1);        // 剩余次数
      const drawDone = ref(false);    // 用完次数后的完成态
      function setDrawMode(m) {
        drawMode.value = m; annoAnchor.value = null;
        if (m !== 'select') { annoLeft.value = drawCount.value; drawDone.value = false; }
      }
      function consumeDraw() {
        if (drawCount.value === 0) return;              // 无限次: 不递减
        annoLeft.value = Math.max(0, annoLeft.value - 1);
        if (annoLeft.value <= 0) { drawDone.value = true; drawMode.value = 'select'; annoAnchor.value = null; }
      }
      function continueDraw() { annoLeft.value = drawCount.value; drawDone.value = false; }
      function clearAnnos() {
        annos.value = []; annoAnchor.value = null; drawDone.value = false;
        annoLeft.value = drawCount.value; syncAnnos(); draw(true);
      }
      // 需求3: 划线数据落库, 复盘历史对局同步渲染
      function syncAnnos() {
        const s = session.value;
        if (!s || !s.session_id) return;
        api('/training/' + s.session_id + '/annos', {
          method: 'POST', body: JSON.stringify({ annos: annos.value }),
        }).catch(function () {});
      }

      const form = reactive({
        category: 'stock', bars: 250, initial_capital: 1000000,
        leverage: 1, strategy_id: null, code: '', start_date: '',
      });
      const notice = ref('');     // 通用提示 (多条命中 / 窗口按可用量收缩)

      // ===== 起始日期可用区间提示 (与后端 MIN_BARS / 按可用量收缩 对齐) =====
      const MIN_BARS_HINT = 20;
      const availHint = ref('');
      let availKey = '';
      async function loadAvail() {
        const code = (codeCode.value || form.code || '').trim();
        availHint.value = '';
        if (!code || !form.start_date) { availKey = ''; return; }
        const key = code + '@' + form.start_date;
        if (key === availKey) return;
        try {
          const bars = (await api('/stocks/' + encodeURIComponent(code) + '/kline?days=2500')) || [];
          const n = bars.filter(function (b) { return String(b.date).slice(0, 10) >= form.start_date; }).length;
          if (!n) availHint.value = '⚠ 该日期晚于标的全部 K 线, 请调整起始日期';
          else if (n < MIN_BARS_HINT) availHint.value = '⚠ 该日期之后仅 ' + n + ' 根 K 线, 不足单局最少 ' + MIN_BARS_HINT + ' 根';
          else availHint.value = '✓ 该日期之后可用 ' + n + ' 根 K 线 (本局按 ' + Math.min(form.bars, n) + ' 根进行)';
          availKey = key;
        } catch (e) { availHint.value = ''; availKey = ''; }
      }

      // ===== 需求2: 指定标的搜索 (代码 / 名称 / 拼音, 类比行情个股搜索) =====
      const codeKw = ref('');
      const codeResults = ref([]);
      const codeOpen = ref(false);
      const codeLabel = ref('');
      const codeCode = ref('');
      let codeTimer = null;
      function onCodeInput() {
        codeCode.value = ''; codeLabel.value = ''; form.code = '';
        availHint.value = ''; availKey = '';
        const k = (codeKw.value || '').trim();
        if (codeTimer) clearTimeout(codeTimer);
        if (!k) { codeResults.value = []; codeOpen.value = false; return; }
        codeTimer = setTimeout(async function () {
          try {
            codeResults.value = (await api('/stocks/search?keyword=' + encodeURIComponent(k) + '&limit=8')) || [];
            codeOpen.value = codeResults.value.length > 0;
          } catch (e) { codeResults.value = []; codeOpen.value = false; }
        }, 250);
      }
      function onCodeFocus() { if (codeResults.value.length) codeOpen.value = true; }
      function pickCode(r) {
        if (!r || !r.code) return;
        codeCode.value = r.code;
        codeLabel.value = (r.name || '') + ' ' + r.code;
        codeKw.value = codeLabel.value;
        form.code = r.code;
        codeResults.value = []; codeOpen.value = false;
        loadAvail();
      }
      function pickCodeFirst() { if (codeResults.value.length) pickCode(codeResults.value[0]); }
      function closeCodeList() { codeOpen.value = false; }
      function clearCodeKw() {
        codeKw.value = ''; codeCode.value = ''; codeLabel.value = '';
        codeResults.value = []; codeOpen.value = false; form.code = '';
        availHint.value = ''; availKey = '';
      }
      /** 提交前解析: 纯代码直通(去掉 sh/sz/bj 前缀, 后端只认裸 6 位代码), 文本/拼音走搜索取首条 */
      async function resolveCodeForSubmit() {
        const raw = (codeKw.value || '').trim();
        if (!raw) { form.code = ''; return null; }
        const m = raw.match(/^(sh|sz|bj)?(\d{4,6})$/i);
        if (m) { form.code = m[2]; return m[2]; }
        if (codeCode.value) { form.code = codeCode.value; return codeCode.value; }
        const rs = (await api('/stocks/search?keyword=' + encodeURIComponent(raw) + '&limit=8')) || [];
        if (!rs.length) throw new Error('未找到匹配标的「' + raw + '」, 请检查名称 / 代码 / 拼音');
        form.code = rs[0].code;
        if (rs.length > 1) {
          notice.value = '「' + raw + '」命中 ' + rs.length + ' 个标的, 已取首个: ' +
                         (rs[0].name || '') + ' ' + rs[0].code;
        }
        return rs[0].code;
      }

      // ===== 需求1+4: 自定义日历弹层 (整块热区 / 年月快切 / 快捷范围 / 深浅主题自适应) =====
      const dateOpen = ref(false);
      const dateField = ref(null);
      const datePopStyle = reactive({ left: '0px', top: '0px', width: '290px' });
      const dowLabels = ['一', '二', '三', '四', '五', '六', '日'];
      const quickRanges = [
        { key: 'today', label: '今天' },
        { key: 'm1', label: '近 1 月' },
        { key: 'm3', label: '近 3 月' },
        { key: 'y1', label: '近 1 年' },
      ];
      const DATE_MIN = '2015-01-01';       // 有效区间下限 (早于此通常无行情数据)
      function localDateStr(d) {
        return d.getFullYear() + '-' +
               String(d.getMonth() + 1).padStart(2, '0') + '-' +
               String(d.getDate()).padStart(2, '0');
      }
      const todayStr = localDateStr(new Date());
      const DATE_MAX = todayStr;           // 上限: 今日 (训练基于历史数据回放)
      const calY = ref(new Date().getFullYear());
      const calM = ref(new Date().getMonth() + 1);   // 1-12
      const calCells = computed(function () {
        const y = calY.value, m = calM.value;
        const lead = (new Date(y, m - 1, 1).getDay() + 6) % 7;   // 周一为一周起始
        const daysInMonth = new Date(y, m, 0).getDate();
        const prevDays = new Date(y, m - 1, 0).getDate();
        const cells = [];
        for (let i = 0; i < 42; i++) {
          const n = i - lead + 1;
          let yy = y, mm = m, dd = n, inMonth = true;
          if (n <= 0) { mm = m - 1; if (mm === 0) { mm = 12; yy = y - 1; } dd = prevDays + n; inMonth = false; }
          else if (n > daysInMonth) { mm = m + 1; if (mm === 13) { mm = 1; yy = y + 1; } dd = n - daysInMonth; inMonth = false; }
          const v = yy + '-' + String(mm).padStart(2, '0') + '-' + String(dd).padStart(2, '0');
          cells.push({
            key: v, day: dd, v: v, inMonth: inMonth,
            isToday: v === todayStr, isSel: v === form.start_date,
            disabled: v < DATE_MIN || v > DATE_MAX,
          });
        }
        return cells;
      });
      const calTip = computed(function () {
        return form.start_date
          ? '已选 ' + form.start_date + ' · 与标的同填即为非双盲训练'
          : '上限为今日 · 早于 ' + DATE_MIN + ' 的日期无效';
      });
      function computePos() {
        const el = dateField.value;
        if (!el) return;
        const r = el.getBoundingClientRect();
        if (!r.width) return;
        const vw = window.innerWidth;
        const w = Math.min(Math.max(290, Math.round(r.width)), vw - 24);
        datePopStyle.width = w + 'px';
        datePopStyle.left = Math.max(12, Math.min(Math.round(r.left), vw - w - 12)) + 'px';
        const need = 340;   // 弹层预估高度
        const below = window.innerHeight - r.bottom;
        if (below < need && r.top > below) {
          datePopStyle.bottom = Math.round(window.innerHeight - r.top + 6) + 'px';
          datePopStyle.top = 'auto';
        } else {
          datePopStyle.top = Math.round(r.bottom + 6) + 'px';
          datePopStyle.bottom = 'auto';
        }
      }
      function openCalendar() {
        if (form.start_date) {
          const p = form.start_date.split('-');
          calY.value = Number(p[0]); calM.value = Number(p[1]);
        }
        dateOpen.value = true;
        codeOpen.value = false;
        nextTick(computePos);
      }
      function toggleCalendar() { if (dateOpen.value) closeCalendar(); else openCalendar(); }
      function closeCalendar() { dateOpen.value = false; }
      function stepMonth(d) {
        let m = calM.value + d;
        while (m > 12) { m -= 12; calY.value += 1; }
        while (m < 1) { m += 12; calY.value -= 1; }
        calM.value = m;
      }
      function stepYear(d) { calY.value += d; }
      function pickDate(c) {
        if (!c || c.disabled) return;
        form.start_date = c.v;
        dateOpen.value = false;
        availKey = ''; loadAvail();
      }
      function clearDate() {
        form.start_date = ''; availHint.value = ''; availKey = ''; dateOpen.value = false;
      }
      function quickPick(q) {
        const now = new Date();
        const d = q.key === 'today' ? now
                : q.key === 'y1' ? new Date(now.getFullYear() - 1, now.getMonth(), now.getDate())
                : new Date(now.getFullYear(), now.getMonth() - (q.key === 'm1' ? 1 : 3), now.getDate());
        let v = localDateStr(d);
        if (v > DATE_MAX) v = DATE_MAX;
        if (v < DATE_MIN) v = DATE_MIN;
        pickDate({ v: v, disabled: false });
      }

      let chart = null;
      let intradayChart = null;

      // 市场背景 (前置K线)
      const preBars = ref([]);
      const preCount = computed(function () { return preBars.value.length; });
      const preDate = ref('');
      const preAmp = ref(null);

      const progressPct = computed(function () {
        const s = session.value;
        return s && s.bars_total ? Math.round(s.bars_used / s.bars_total * 100) : 0;
      });
      const pnlClass = computed(function () {
        const p = session.value ? session.value.pnl : 0;
        return p > 0 ? 'up' : p < 0 ? 'down' : '';
      });
      // 满仓后可用资金不足 1 手 → 禁止买入 (与后端资金校验一致, 避免"满仓还能再买")
      // 已有待成交委托时同样禁用 (次日开盘才会成交, 避免重复挂单)
      const canBuy = computed(function () {
        const s = session.value;
        if (!s || s.status !== 'active') return false;
        if (s.pending_buy) return false;
        const price = s.current_price || 0;
        return price > 0 && (s.cash || 0) >= price * 100;
      });
      // T+1: 当日买入的持仓不可卖出 → 无可卖数量时禁用卖出按钮
      const sellLocked = computed(function () {
        const s = session.value;
        return !!(s && s.status === 'active' && s.pos > 0 && (s.sellable_qty || 0) <= 0);
      });

      // 单局 K 线根数上限: 由后端品类元数据驱动 (股票双盲 1250(含长周期) / 妖股训练 250)
      const curCat = computed(function () {
        return categories.value.find(function (c) { return c.key === form.category; }) || null;
      });
      const barsMax = computed(function () {
        return (curCat.value && curCat.value.bars_max) || 250;
      });
      // 滑块步长固定 10: 需整除 (barsMax - 20), 否则拖不到上限 (如 1250)
      const barsStep = 10;

      function pickCategory(c) {
        form.category = c.key;
        form.leverage = c.leverage || 1;
        // 切换品类 → 根数取其默认值, 并夹取到该品类上限 (显示与实际提交保持一致)
        const cap = c.bars_max || 250;
        const def = c.bars_default || Math.min(form.bars || 250, cap);
        form.bars = Math.min(Math.max(def, 20), cap);
      }

      async function loadCategories(force) {
        try {
          // 需求101701-2: 强制绕过浏览器 HTTP 缓存 (移动端曾缓存旧品类列表而看不到「长周期训练」);
          // force=true 时同时清前端内存缓存, 保证每次进入训练模块都拿到最新品类。
          if (force) { window.__apiCacheClear('/training/categories'); }
          categories.value = (await api('/training/categories', { method: 'GET' },
                                       { httpNoStore: true })).categories;
        } catch (e) { console.error('加载品类失败', e.message); }
      }
      // 归属战法默认「默认」(未归类 = null): 后端不再自动创建/绑定「副本」,
      // 界面也不应凭空出现用户没建过的战法
      function defaultStrategyId() {
        return null;
      }
      async function loadStrategies(selectDefault) {
        try {
          strategies.value = (await api('/strategies')).strategies;
          if (selectDefault) form.strategy_id = defaultStrategyId();
        } catch (e) { console.error('加载战法失败', e.message); }
      }
      async function addStrategy() {
        const name = (newStrategyName.value || '').trim();
        if (!name) return;
        try {
          const st = await api('/strategies', { method: 'POST', body: JSON.stringify({ name: name }) });
          await loadStrategies();
          form.strategy_id = st.id;
          newStrategyName.value = '';
        } catch (e) { error.value = e.message; }
      }
      // 删除战法: 仅取消训练记录上的标签, 不会删除任何训练记录
      async function delStrategy(id) {
        if (!id) return;
        const s = strategies.value.find(function (x) { return x.id === id; });
        const name = s ? s.name : '';
        if (!window.confirm('确认删除战法「' + name + '」?\n仅取消训练记录上的该标签, 不会删除任何训练记录。')) return;
        try {
          await api('/strategies/' + id, { method: 'DELETE' });
          const wasSelected = form.strategy_id === id;
          await loadStrategies();
          if (wasSelected) form.strategy_id = defaultStrategyId();
          savedMsg.value = '✅ 已删除战法「' + name + '」(训练记录已保留)';
        } catch (e) { error.value = e.message; }
      }

      function ensureChart() {
        return nextTick().then(function () {
          return waitForSize('train-kline');
        }).then(function (el) {
          if (!el) return null;
          if (chart) { chart.dispose(); chart = null; }
          // 需求101604: useDirtyRect 只重绘变化区域, 推进/切周期/加自选等重绘更快
          chart = echarts.init(el, null, { useDirtyRect: true });
          return chart;
        });
      }

      // ---------- 右侧分时 (同花顺风格, 联动主图任意K线) ----------
      function disposePre() {
        try { intradayChart && intradayChart.dispose(); } catch (e) {}
        intradayChart = null;
      }
      function ensureIntradayChart() {
        return nextTick().then(function () { return waitForSize('train-intraday'); })
          .then(function (el) {
            if (!el) return null;
            if (intradayChart) { intradayChart.dispose(); intradayChart = null; }
            intradayChart = echarts.init(el);
            return intradayChart;
          });
      }

      // 需求101701: 分时 URL 携带版本令牌 —— 令牌与「内容」一一对应,
      // 使同一 URL 的内容恒定, 从而让只读分时可被前端预加载层长缓存:
      //   · 背景K线: 内容只随 (周期 + 揭晓态) 变化 → 令牌稳定
      //   · 已完成周期桶: 内容只随 (周期 + 揭晓态) 变化 → 令牌稳定, 历史K线分时可长期命中缓存
      //   · 当前正在形成的周期桶: OHLC 随已推进交易日数变化 → 令牌含 days_used, 保证始终新鲜
      function intradayUrl(kind, idx) {
        const s = session.value;
        if (!s) return '';
        const rev = s.code_revealed ? 1 : 0;
        const sid = s.session_id;
        if (kind === 'pre') {
          return '/training/session/' + sid + '/pre-intraday?idx=' + idx + '&v=' + [s.period, rev].join('-');
        }
        const isPartial = idx >= s.bars_used;
        const v = isPartial
          ? [s.period, 'p', s.days_used, rev].join('-')
          : [s.period, rev].join('-');
        return '/training/session/' + sid + '/bar-intraday?idx=' + idx + '&v=' + v;
      }

      // 统一加载分时: mergedIdx 主图合并索引 (<preN=前置背景, >=preN=训练K线)
      async function loadIntraday(mergedIdx) {
        const s = session.value;
        if (!s) return;
        const preN = preBars.value.length;
        let d = null, zone = '';
        try {
          if (mergedIdx < preN) {
            const pi = Math.max(0, Math.min(mergedIdx, preN - 1));
            d = await api(intradayUrl('pre', pi));
            zone = '📒背景';
          } else {
            const n = (s.bars || []).length;
            let bi = mergedIdx - preN;
            if (bi > n - 1) bi = n - 1;   // 容错: 索引越界收敛到末根, 避免 400
            if (bi < 0) bi = 0;
            d = await api(intradayUrl('bar', bi));
            zone = '🎯训练';
          }
        } catch (e) { console.error('分时失败:', e.message); return; }
        preDate.value = (s.code_revealed ? '' : zone + ' · ') + d.date;
        const pc = d.prev_close;
        preAmp.value = (d.amplitude != null) ? d.amplitude
          : (pc && d.high != null && d.low != null) ? Number(((d.high - d.low) / pc * 100).toFixed(2)) : null;
        const c = await ensureIntradayChart();
        if (c && d.points && d.points.length) drawIntraday(d);
        else if (c) drawIntradayEmpty(d);
      }

      // 展示当前训练K线 (bars_used) 的当日分时
      async function showCurrentIntraday() {
        const s = session.value;
        if (!s) return;
        // 周/月/年线首桶未走完时训练段无可见K线 → 无对应分时可加载
        if (!(s.bars || []).length) return;
        let ci = s.bars_used;
        if (ci >= s.bars_total) ci = s.bars_total - 1;
        if (ci < 0) return;
        await loadIntraday(preBars.value.length + ci);
      }

      // 同花顺风格分时: 上=价格+均价+昨收虚线, 右轴涨跌幅%, 下=成交量
      function drawIntraday(d) {
        if (!intradayChart || !d.points || !d.points.length) return;
        const times = d.points.map(function (p) { return p.time; });
        const prices = d.points.map(function (p) { return p.price; });
        const avgs = d.points.map(function (p) { return p.avg; });
        const vols = d.points.map(function (p) { return p.volume; });
        const pc = d.prev_close || d.points[0].price;
        let up = pc * 0.005, dn = pc * 0.005;
        for (let i = 0; i < prices.length; i++) {
          up = Math.max(up, prices[i] - pc, avgs[i] - pc);
          dn = Math.max(dn, pc - prices[i], pc - avgs[i]);
        }
        const maxDev = Math.max(up, dn) * 1.15;
        const pctFmt = function (v) {
          const pct = ((v - pc) / pc * 100);
          return (pct > 0 ? '+' : '') + pct.toFixed(2) + '%';
        };
        intradayChart.setOption({
          backgroundColor: 'transparent', animation: false,
          tooltip: {
            trigger: 'axis', axisPointer: { type: 'cross' },
            formatter: function (params) {
              const i = params[0].dataIndex; const p = d.points[i];
              const chg = ((p.price - pc) / pc * 100).toFixed(2);
              return p.time + '<br/>价格 ' + p.price.toFixed(2) + ' (' + (chg > 0 ? '+' : '') + chg + '%)<br/>均价 ' + p.avg.toFixed(2) + '<br/>量 ' + fmt(p.volume);
            },
          },
          grid: [
            { left: 56, right: 64, top: 16, height: '58%' },
            { left: 56, right: 64, top: '75%', height: '18%' },
          ],
          xAxis: [
            { type: 'category', data: times, boundaryGap: false, gridIndex: 0, axisLine: { lineStyle: { color: tc('--border-2', '#2a2f4a') } },
              axisLabel: { color: tc('--text-dim', '#8892b0'), fontSize: 10,
                formatter: function (v, i) { return (i % 30 === 0) ? v : ''; } } },
            { type: 'category', data: times, gridIndex: 1, axisLine: { show: false }, axisLabel: { show: false } },
          ],
          yAxis: [
            { scale: true, min: pc - maxDev, max: pc + maxDev, gridIndex: 0, axisLine: { show: false },
              splitLine: { lineStyle: { color: tc('--border', '#262b4d') } },
              axisLabel: { color: tc('--text-strong', '#d7dce2'), fontSize: 10, formatter: function (v) { return v.toFixed(2); } } },
            // 右轴: 涨跌幅% (同花顺风格, 红涨绿跌)
            { scale: true, min: pc - maxDev, max: pc + maxDev, gridIndex: 0, position: 'right',
              axisLine: { show: false }, splitLine: { show: false },
              axisLabel: { fontSize: 10, formatter: pctFmt,
                color: function (v) { return v > pc ? tc('--up', '#ff4757') : v < pc ? tc('--down', '#2ed573') : tc('--text-dim', '#8892b0'); } } },
            { scale: true, gridIndex: 1, axisLine: { show: false }, splitLine: { show: false }, axisLabel: { show: false } },
          ],
          series: [
            { name: '价格', type: 'line', data: prices, smooth: true, xAxisIndex: 0, yAxisIndex: 0, showSymbol: false,
              lineStyle: { color: tc('--brand-2', '#26a7ff'), width: 1.6 },
              areaStyle: { color: tc('--brand-soft', 'rgba(38,167,255,0.12)') },
              markLine: { silent: true, symbol: 'none',
                label: { show: true, color: tc('--text-dim', '#8892b0'), fontSize: 9, formatter: '昨收', position: 'insideEndTop' },
                lineStyle: { color: tc('--text-dim', '#8892b0'), type: 'dashed', width: 1 }, data: [{ yAxis: pc }] } },
            { name: '均价', type: 'line', data: avgs, smooth: true, xAxisIndex: 0, yAxisIndex: 0, showSymbol: false,
              lineStyle: { color: tc('--gold', '#ffd04b'), width: 1.2 } },
            { name: '成交量', type: 'bar', data: vols, xAxisIndex: 1, yAxisIndex: 2,
              itemStyle: { color: function (p) { return prices[p.dataIndex] >= pc ? tc('--up', '#ff4757') : tc('--down', '#2ed573'); } } },
          ],
        }, true);
        intradayChart.resize();
      }

      // 分时空数据样式: 当训练该日无真实分钟数据时展示占位信息
      function drawIntradayEmpty(d) {
        if (!intradayChart) return;
        intradayChart.clear();
        const reason = d.suspended ? '该日停牌' : '该日无真实分钟数据';
        intradayChart.setOption({
          backgroundColor: 'transparent', animation: false,
          graphic: [{
            type: 'text', left: 'center', top: '42%',
            style: {
              text: '⏸ ' + reason + '\n' +
                    '开 ' + d.open + '   高 ' + d.high + '\n' +
                    '低 ' + d.low + '   收 ' + d.close,
              fill: '#6a7190', fontSize: 13, lineHeight: 22, textAlign: 'center',
            },
          }],
        }, true);
        intradayChart.resize();
      }

      function resetForm() {
        Object.assign(form, { category: 'stock', bars: 250, initial_capital: 1000000, leverage: 1, code: '', start_date: '' });
        form.strategy_id = defaultStrategyId();
        codeKw.value = ''; codeCode.value = ''; codeLabel.value = ''; codeResults.value = []; codeOpen.value = false;
        availHint.value = ''; availKey = ''; dateOpen.value = false; notice.value = '';
        resetMa();
      }
      function clearCode() {
        form.code = ''; form.start_date = '';
        codeKw.value = ''; codeCode.value = ''; codeLabel.value = ''; codeResults.value = []; codeOpen.value = false;
        availHint.value = ''; availKey = ''; dateOpen.value = false;
      }

      // ===== 需求101802-1: 训练标的池 (弹窗展示池内个股 + 筛选/分页) =====
      const poolOpen = ref(false);
      const poolRows = ref([]);
      const poolTotal = ref(0);
      const poolFilters = ref([]);
      const poolFilter = ref('all');
      // 需求101808-3: 标的池按品类分别展示 (stock/demon)
      const poolCategory = ref('stock');
      const poolLoading = ref(false);
      const poolOffset = ref(0);
      const POOL_PAGE = 30;
      const poolHasMore = computed(function () {
        return poolRows.value.length < poolTotal.value;
      });
      function poolPct(v) {
        if (v == null) return '--';
        return (Number(v) * 100).toFixed(2) + '%';
      }
      async function loadPool(reset) {
        poolLoading.value = true;
        try {
          if (reset) { poolOffset.value = 0; window.__apiCacheClear('/training/pool'); }
          const q = '/training/pool?filter=' + encodeURIComponent(poolFilter.value) +
                    '&category=' + encodeURIComponent(poolCategory.value) +
                    '&limit=' + POOL_PAGE + '&offset=' + poolOffset.value;
          const res = await api(q, { method: 'GET' }, { httpNoStore: true });
          poolTotal.value = res.total || 0;
          poolFilters.value = res.filters || [];
          const rows = res.rows || [];
          poolRows.value = reset ? rows : poolRows.value.concat(rows);
          poolOffset.value = poolRows.value.length;
        } catch (e) {
          if (reset) { poolRows.value = []; poolTotal.value = 0; }
        } finally { poolLoading.value = false; }
      }
      function openPool() {
        poolOpen.value = true;
        // 需求101808-3: 默认对齐当前训练品类, 再按品类分别展示
        if (categories.value.some(function (c) { return c.key === form.category; })) {
          poolCategory.value = form.category;
        }
        loadPool(true);
      }
      function closePool() { poolOpen.value = false; }
      function pickPoolFilter(k) {
        if (poolFilter.value === k) return;
        poolFilter.value = k;
        loadPool(true);
      }
      /** 需求101808-3: 切换标的池品类 (stock/demon), 重新拉取该品类池 */
      function pickPoolCategory(k) {
        if (poolCategory.value === k) return;
        poolCategory.value = k;
        loadPool(true);
      }
      function poolMore() { loadPool(false); }
      /** 池内点选 → 设为「指定标的」, 下次开局使用该标的 (关闭弹窗回到配置面板) */
      function usePoolRow(r) {
        if (!r || !r.code) return;
        codeCode.value = r.code;
        codeLabel.value = (r.name || '') + ' ' + r.code;
        codeKw.value = codeLabel.value;
        form.code = r.code;
        codeResults.value = []; codeOpen.value = false;
        closePool();
        notice.value = '已从标的池选择：' + codeLabel.value;
        loadAvail();
      }

      // 需求101604: 开局请求体 (start / prime 必须构造完全一致, 后端靠参数指纹复用预生成数据)
      function startBody() {
        return {
          category: form.category, bars: form.bars,
          initial_capital: form.initial_capital,
          leverage: form.leverage,
          strategy_id: form.strategy_id || null,
          code: (form.code || '').trim() || null,
          start_date: (form.start_date || '').trim() || null,
        };
      }

      // 需求101604: 预生成 —— 进入训练页 / 改参数 400ms 后, 提前把本局数据备好
      let primeTimer = null, primeSeq = 0;
      async function prime() {
        if (session.value) return;                 // 局中不预生成
        refreshLogin();
        if (!loggedIn.value) return;
        const seq = ++primeSeq;
        try {
          await resolveCodeForSubmit();
          if (seq !== primeSeq) return;            // 参数又变了 → 丢弃过期请求
          await api('/training/prime', { method: 'POST', body: JSON.stringify(startBody()) });
        } catch (e) { /* 预生成失败不影响正常开局 (start 会现场构建) */ }
      }
      function schedulePrime() {
        if (primeTimer) clearTimeout(primeTimer);
        primeTimer = setTimeout(function () { primeTimer = null; prime(); }, 400);
      }

      async function start() {
        refreshLogin();
        if (!loggedIn.value) { error.value = '请先登录后再开始训练'; return; }
        loading.value = true; error.value = ''; notice.value = '';
        try {
          await resolveCodeForSubmit();   // 需求2: 代码/名称/拼音 → 归一化标的代码
          const res = await api('/training/start', { method: 'POST', body: JSON.stringify(startBody()) });
          marks.value = [];
          note.value = ''; savedMsg.value = '';
          qty.value = null;
          condPrice.value = null; condTrail.value = null; condKind.value = 'take_profit'; condRatio.value = 1;
          annos.value = []; annoAnchor.value = null; drawMode.value = 'select'; drawDone.value = false;
          session.value = res;
          // 需求3: 起始日期贴近数据末端时后端会按可用量收缩窗口 → 明确告知实际根数
          if (res.non_blind && res.bars_total < form.bars) {
            notice.value = '本局按 ' + res.bars_total + ' 根 K 线进行 (' + res.start_date + ' ~ ' + res.end_date + ')';
          }
          // 市场背景: 训练窗口之前的历史K线 (与训练K线合并连续绘制)
          preBars.value = res.pre_bars || [];
          preAmp.value = null; preDate.value = '';
          loading.value = false;             // 需求101604: API 已返回, 立即解除「开始中」遮罩
          await ensureChart();
          draw(false, true);                 // 需求101604: 首帧只画 K线+成交量 → 主图立即可见
          // 下一帧补齐 MA/MACD/买卖点/画线, 并异步加载右侧分时 (不阻塞首屏)
          requestAnimationFrame(function () {
            draw(false, false);
            showCurrentIntraday();
          });
        } catch (e) {
          error.value = '❌ ' + e.message;
        } finally {
          loading.value = false;
        }
      }

      async function act(kind) {
        if (!session.value) return;
        const sid = session.value.session_id;
        try {
          const body = { qty: qty.value || null, ratio: kind === 'buy' ? (positionRatio.value || null) : null };
          const before = session.value.bars_used;
          const res = await api('/training/' + sid + '/' + kind, { method: 'POST', body: JSON.stringify(body) });
          if (kind === 'sell') {
            const b = session.value.bars[before];
            // 卖出 = 当日收盘即时成交 → 立即记录卖点
            marks.value.push({
              index: before, date: b ? b.date : null, action: 'SELL', price: res.current_price,
            });
          } else if (kind === 'buy') {
            // 需求4: 买入为挂单, 于次日开盘成交 → 成交时(pending_filled)再补买点
            error.value = '⏳ 买入委托已挂出, 将于次日开盘成交';
          }
          session.value = res;
          const p = showCurrentIntraday();   // 需求101701: 分时请求与 K线绘制并行
          draw();
          await p;
        } catch (e) { error.value = e.message; }
      }

      // 需求4: 推进后按成交回报补买点 / 提示买不到
      function applyFill(res) {
        const pf = res && res.pending_filled;
        if (!pf) return;
        if (pf.action === 'BUY' || pf.action === 'COVER') {
          marks.value.push({
            index: pf.bar_index, date: pf.date,
            action: pf.action === 'COVER' ? 'COVER' : 'BUY', price: pf.price,
          });
        } else if (pf.action === 'BUY_FAILED') {
          error.value = '⚠️ ' + (pf.reason || '无法买入');
        }
      }

      // 挂止盈/止损/移动止损条件单 (不要求先有持仓: 触发即尝试操作, 无可操作持仓则提示)
      async function setConditional() {
        const s = session.value;
        if (!s) return;
        const payload = { ratio: condRatio.value || 1 };
        if (condKind.value === 'trailing_stop') {
          const pct = Number(condTrail.value);
          if (!pct || pct <= 0) { error.value = '请输入有效的回撤幅度(%)'; return; }
          payload.kind = 'trailing_stop';
          payload.trail_pct = pct;
        } else {
          const price = Number(condPrice.value);
          if (!price || price <= 0) { error.value = '请输入有效的触发价'; return; }
          payload.kind = condKind.value;
          payload.trigger_price = price;
        }
        try {
          session.value = await api('/training/' + s.session_id + '/conditional', {
            method: 'POST',
            body: JSON.stringify(payload),
          });
          condPrice.value = null; condTrail.value = null;
          error.value = ''; condNotice.value = '';
        } catch (e) { error.value = e.message; }
      }

      async function cancelConditional(id) {
        if (!session.value) return;
        try {
          session.value = await api('/training/' + session.value.session_id + '/conditional/' + id, { method: 'DELETE' });
        } catch (e) { error.value = e.message; }
      }

      // 条件单触发但不可操作 → 提示
      function noteSkipped(res) {
        const sk = (res && res.conditions_skipped) || [];
        condNotice.value = sk.length
          ? sk.map(function (c) { return c.kind_label + '@' + c.triggered_price; }).join('、') + ' 触发, 但当前无可操作持仓, 已跳过'
          : '';
      }

      async function doAdvance() {
        if (!session.value) return;
        try {
          error.value = '';
          const res = await api('/training/' + session.value.session_id + '/advance', { method: 'POST', body: '{}' });
          session.value = res.session;
          applyFill(res);
          noteSkipped(res);
          const p = showCurrentIntraday();   // 需求101701: 分时请求与 K线绘制并行, 缩短等待
          draw();
          await p;
        } catch (e) { error.value = e.message; }
      }

      // 观望 = hold + advance 一步完成 (需求101701: 合并为单次请求, 后端只落库一次)
      async function holdAndAdvance() {
        if (!session.value) return;
        try {
          error.value = '';
          const res = await api('/training/' + session.value.session_id + '/hold?advance=1', { method: 'POST', body: '{}' });
          session.value = res.session;
          applyFill(res);
          noteSkipped(res);
          const p = showCurrentIntraday();
          draw();
          await p;
        } catch (e) { error.value = e.message; }
      }

      // 观望 5 个交易日 = 批量推进 5 根 (自动结算在末尾触发)
      async function advance5() {
        if (!session.value) return;
        try {
          error.value = '';
          const res = await api('/training/' + session.value.session_id + '/advance?steps=5', { method: 'POST', body: '{}' });
          session.value = res.session;
          applyFill(res);
          noteSkipped(res);
          const p = showCurrentIntraday();
          draw();
          await p;
        } catch (e) { error.value = e.message; }
      }

      // 训练运行中切换周期 (日/周/月/年), 进度不重置
      async function switchPeriod(period) {
        const s = session.value;
        if (!s || s.status !== 'active' || s.period === period) return;
        try {
          session.value = await api('/training/' + s.session_id + '/period', { method: 'POST', body: JSON.stringify({ period: period }) });
          // 周期变化后画线索引失效需清空; 买卖点按日期重定位, 予以保留
          annos.value = []; annoAnchor.value = null;
          syncAnnos();
          preBars.value = session.value.pre_bars || [];
          const p = showCurrentIntraday();
          draw();
          await p;
        } catch (e) { error.value = e.message; }
      }

      async function settle() {
        if (!session.value) return;
        try {
          session.value = await api('/training/' + session.value.session_id + '/settle', { method: 'POST', body: '{}' });
          const p = showCurrentIntraday();
          draw();
          await p;
        } catch (e) { error.value = e.message; }
      }

      function restart() {
        session.value = null; marks.value = []; note.value = '';
        savedMsg.value = ''; error.value = '';
        annos.value = []; annoAnchor.value = null; drawMode.value = 'select';
        preBars.value = [];
        preAmp.value = null; preDate.value = '';
        disposePre();
        if (chart) { chart.dispose(); chart = null; }
        schedulePrime();          // 需求101604: 回到设置面板 → 提前预生成下一局数据
      }

      async function saveStrategy() {
        if (!session.value) return;
        try {
          await api('/training/history/' + session.value.session_id, {
            method: 'PATCH',
            body: JSON.stringify({ strategy_id: form.strategy_id || null }),
          });
          savedMsg.value = '✅ 已归类到「' + (strategies.value.find(function (s) { return s.id === form.strategy_id; }) || {}).name + '」';
        } catch (e) { savedMsg.value = '❌ ' + e.message; }
      }

      async function saveNote() {
        if (!session.value) return;
        try {
          await api('/training/history/' + session.value.session_id, {
            method: 'PATCH',
            body: JSON.stringify({ note: note.value }),
          });
          savedMsg.value = '✅ 复盘笔记已保存';
        } catch (e) { savedMsg.value = '❌ ' + e.message; }
      }

      // 绘图时固定可视窗口根数: 推进时窗口固定迁移(不挤压), 历史K线可左右滑动
      const VIEW_WIN = 80;

      // ---------- 绘图 ----------
      // lite=true 只画「K线+成交量」关键帧 (需求101604: 首屏先出主图, MA/MACD/标记下一帧补齐)
      function draw(keepZoom, lite) {
        const s = session.value;
        if (!chart || !s) return;
        const pre = preBars.value || [];
        const bars = s.bars || [];
        // 周/月/年线在「尚未走完第一个周期桶」时训练段为空, 仍需绘制市场背景 (不能整体 return)
        if (!pre.length && !bars.length) return;
        const revealed = !!s.code_revealed;
        const preN = pre.length;         // 前置段根数 (训练起点偏移)
        // 连成一条序列: 前置K线 + 已可见训练K线, 双盲时横轴连续编号
        const all = pre.concat(bars);
        const x = all.map(function (b, i) {
          if (revealed) return b.date;
          return '#' + (i + 1);
        });
        const ohlc = all.map(function (b) { return [b.open, b.close, b.low, b.high]; });
        const vols = all.map(function (b) { return b.volume; });
        const inTrain = function (i) { return i >= preN; };  // 前置段 vs 训练段

        const series = [{
          name: 'K线', type: 'candlestick', data: ohlc,
          itemStyle: { color: tc('--up', '#ff4757'), color0: tc('--down', '#2ed573'),
                       borderColor: tc('--up', '#ff4757'), borderColor0: tc('--down', '#2ed573') },
          xAxisIndex: 0, yAxisIndex: 0, emphasis: { focus: 'series' },
        }, {
          name: '成交量', type: 'bar', data: vols,
          // 前置背景段与训练段均为红涨绿跌, 前置段略透明以示区分
          itemStyle: { color: function (p) {
            const i = p.dataIndex;
            const up = all[i].close >= all[i].open;
            if (!inTrain(i)) return up ? tc('--up-soft', 'rgba(255,71,87,0.5)') : tc('--down-soft', 'rgba(46,213,115,0.5)');
            return up ? tc('--up', '#ff4757') : tc('--down', '#2ed573');
          } },
          xAxisIndex: 1, yAxisIndex: 1,
        }];

        // MACD 固定第三面板: 优先用后端按日线计算 + 周期桶采样结果 (周/月/年线不再空洞)
        let macdM = null;
        if (s.macd && Array.isArray(s.macd.dif) && s.macd.dif.length === all.length) {
          macdM = s.macd;
        } else if (window.Indicators && window.Indicators.macd) {
          macdM = window.Indicators.macd(all);   // 兼容回退 (旧会话无 macd 字段)
        }
        if (macdM && !lite) {
          const m = macdM;
          series.push(
            { name: 'DIF', type: 'line', data: m.dif, xAxisIndex: 2, yAxisIndex: 2, showSymbol: false, smooth: true,
              lineStyle: { width: 1, color: tc('--warn', '#ffaa00') }, itemStyle: { color: tc('--warn', '#ffaa00') } },
            { name: 'DEA', type: 'line', data: m.dea, xAxisIndex: 2, yAxisIndex: 2, showSymbol: false, smooth: true,
              lineStyle: { width: 1, color: tc('--brand', '#00b5ff') }, itemStyle: { color: tc('--brand', '#00b5ff') } },
            { name: 'MACD', type: 'bar', data: m.hist.map(function (v) {
                return { value: v, itemStyle: { color: (v >= 0) ? tc('--up', '#ff4757') : tc('--down', '#2ed573') } };
              }), xAxisIndex: 2, yAxisIndex: 2 },
          );
        }

        // MA 均线 (重叠加在主K线图, 数据不足时 null 自动跳过)
        if (!lite && window.Indicators && window.Indicators.sma && maOn.value.length) {
          const closes = all.map(function (b) { return b.close; });
          maOn.value.forEach(function (k) {
            const n = parseInt(k, 10);
            if (!n) return;
            series.push({
              name: 'MA' + n, type: 'line', data: window.Indicators.sma(closes, n),
              xAxisIndex: 0, yAxisIndex: 0, showSymbol: false, smooth: true,
              lineStyle: { width: 1, color: MA_COLORS[k] },
              itemStyle: { color: MA_COLORS[k] },
            });
          });
        }

        // 画线 overlay (自定义系列: 水平线/线段/延长线/射线/矩形, 随缩放平移实时重绘)
        if (!lite && annos.value.length) {
          series.push({
            name: '画线', type: 'custom', xAxisIndex: 0, yAxisIndex: 0,
            z: 30, silent: true, animation: false,
            // dataZoom 默认 filterMode='filter' 会过滤掉可视窗口外的数据点;
            // 若只放一个点(如索引0), 默认窗口贴住最新K线时该点被过滤 → renderItem 不被调用 → 画线不显示。
            // 故用覆盖全量索引的数据点, 保证窗口内始终有数据点驱动 renderItem 绘制。
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
              annos.value.forEach(function (a) {
                if (a.type === 'hline') {
                  const p = toPx(a.x, a.y);
                  if (!p) return;
                  children.push({
                    type: 'line',
                    shape: { x1: rect.x, y1: p[1], x2: rect.x + rect.width, y2: p[1] },
                    style: { stroke: ANNO_RED, lineWidth: 1.4 },   // 同花顺红色
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
                    // 对标同花顺形态统计: 描边 + 虚线, 不填充 → 不遮挡 K 线
                    style: { fill: 'transparent', stroke: ANNO_RED, lineWidth: 1.2, lineDash: [4, 3] },
                  });
                } else if (a.type === 'arrow') {
                  // 箭头: 初始段细 → 末端粗 (分段逼近), 末端实心三角
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
                  } else {   // line: 双向延长, 点状样式
                    pushLine([p1[0] - ux * FAR, p1[1] - uy * FAR], [p1[0] + ux * FAR, p1[1] + uy * FAR], ANNO_RED, ANNO_DASH);
                  }
                }
              });
              return { type: 'group', children: children };
            },
          });
        }

        // 买卖点标记: 优先按日期定位到当前周期的K线 (切换周期后仍保留), 无日期时回退旧索引
        const mp = lite ? [] : marks.value.map(function (m) {
          let di = -1;
          if (m.date) {
            for (let i = preN; i < all.length; i++) {
              if (all[i].date >= m.date) { di = i; break; }
            }
            // 日期超出当前可见范围(如周线尚未聚合到该日) → 落在最后一根, 不丢点
            if (di < 0) di = all.length - 1;
          }
          if (di < 0) di = preN + (m.index || 0);
          if (di < 0) return null;
          if (di >= all.length) di = all.length - 1;   // 容错: 越界收敛到末根而非丢弃
          return {
            name: m.action, value: m.action,
            xAxis: di, yAxis: m.price,
            symbol: m.action === 'BUY' ? 'triangle' : 'arrow',
            symbolSize: 14, symbolRotate: m.action === 'BUY' ? 0 : 180,
            itemStyle: { color: m.action === 'BUY' ? tc('--up', '#ff4757') : tc('--down', '#2ed573') },
            label: { show: true, formatter: m.action === 'BUY' ? 'B' : 'S', color: '#fff', fontSize: 9 },
          };
        }).filter(function (x) { return x; });

        // 条件单触发点: 止盈/止损触发即平仓, 在主图标注 TP / SL
        const conds = (s.conditionals || []);
        if (!lite) conds.forEach(function (c) {
          if (c.status !== 'triggered' || !c.triggered_date) return;
          let di = -1;
          for (let i = preN; i < all.length; i++) {
            if (all[i].date >= c.triggered_date) { di = i; break; }
          }
          if (di < 0) di = all.length - 1;
          const isTp = c.kind === 'take_profit';
          mp.push({
            name: isTp ? 'TP' : 'SL', value: isTp ? '止盈' : '止损',
            xAxis: di, yAxis: c.triggered_price || all[di].close,
            symbol: isTp ? 'pin' : 'pin', symbolSize: isTp ? 30 : 28,
            itemStyle: { color: isTp ? tc('--gold', '#ffd04b') : tc('--brand-2', '#26a7ff') },
            label: { show: true, formatter: isTp ? 'TP' : 'SL', color: tc('--bg', '#1a1f3d'), fontSize: 9, fontWeight: 'bold' },
          });
        });
        if (mp.length) series[0].markPoint = { silent: true, data: mp };

        // 训练起点分隔线 + 训练段背景高亮 (附加到主K线 series, 标注前置段/训练段分界)
        if (preN > 0 && !lite) {
          series[0].markLine = {
            silent: true, symbol: 'none',
            lineStyle: { color: '#e6a23c', type: 'dashed', width: 1 },
            label: { show: true, formatter: '训练开始 ▸', color: '#e6a23c', fontSize: 10, position: 'start' },
            data: [{ xAxis: preN - 0.5 }],
          };
          series[0].markArea = {
            silent: true,
            data: [[{ xAxis: preN, itemStyle: { color: 'rgba(230,162,60,0.05)' } }, { xAxis: all.length - 1 }]],
          };
        }

        // 可视窗口: 默认固定迁移到最新 VIEW_WIN 根; keepZoom 时保留用户当前平移位置
        let zStart = Math.max(0, all.length - VIEW_WIN), zEnd = all.length - 1;
        if (keepZoom && chart) {
          try {
            const z = chart.getOption().dataZoom;
            if (z && z.length && z[0].startValue != null && z[0].endValue != null) {
              zStart = z[0].startValue; zEnd = z[0].endValue;
            }
          } catch (e) {}
        }

        chart.setOption({
          backgroundColor: 'transparent',
          animation: false,
          tooltip: {
            trigger: 'axis', axisPointer: { type: 'cross' },
            formatter: function (params) {
              const i = params[0].dataIndex;
              const k = all[i];
              if (!k) return '';
              const chg = i > 0 ? ((k.close - all[i - 1].close) / all[i - 1].close * 100).toFixed(2) : '--';
              const zone = inTrain(i) ? '🎯 训练' : '📊 背景';
              return zone + ' · <b>' + x[i] + '</b><br/>开 ' + k.open + ' 收 ' + k.close + ' (' + chg + '%)<br/>高 ' + k.high + ' 低 ' + k.low + '<br/>量 ' + fmt(k.volume);
            },
          },
          legend: { show: false },
          grid: [
            { left: 52, right: 16, top: 14, height: '40%' },
            { left: 52, right: 16, top: '57%', height: '8%' },
            { left: 52, right: 16, top: '68%', height: '15%' },
          ],
          xAxis: [
            { type: 'category', data: x, gridIndex: 0, axisLine: { lineStyle: { color: tc('--border-2', '#2a2f4a') } }, axisLabel: { color: tc('--text-dim', '#888'), fontSize: 10 } },
            { type: 'category', data: x, gridIndex: 1, axisLine: { show: false }, axisLabel: { show: false } },
            { type: 'category', data: x, gridIndex: 2, axisLine: { lineStyle: { color: tc('--border-2', '#2a2f4a') } }, axisLabel: { color: tc('--text-mute', '#666'), fontSize: 9 } },
          ],
          yAxis: [
            { scale: true, gridIndex: 0, axisLine: { show: false }, splitLine: { lineStyle: { color: tc('--border', '#1e2347') } }, axisLabel: { color: tc('--text-dim', '#888'), fontSize: 10 } },
            { scale: true, gridIndex: 1, axisLine: { show: false }, splitLine: { show: false }, axisLabel: { show: false } },
            { scale: true, gridIndex: 2, axisLine: { show: false }, splitLine: { lineStyle: { color: tc('--border', '#1e2347') } }, axisLabel: { color: tc('--text-dim', '#888'), fontSize: 9 } },
          ],
          series: series,
          dataZoom: [
            { type: 'inside', xAxisIndex: [0, 1, 2],
              startValue: zStart, endValue: zEnd,
              zoomOnMouseWheel: false, moveOnMouseWheel: true },
            { type: 'slider', xAxisIndex: [0, 1, 2],
              startValue: zStart, endValue: zEnd,
              bottom: 3, height: 26,
              borderColor: tc('--border-2', '#262b4d'), backgroundColor: tc('--slider-bg', 'rgba(38,42,77,0.4)'),
              fillerColor: tc('--slider-fill', 'rgba(38,167,255,0.15)'),
              handleStyle: { color: tc('--brand-2', '#26a7ff') },
              textStyle: { color: tc('--text-dim', '#8892b0'), fontSize: 9 } },
          ],
        }, true);
        chart.resize();
        bindChartClick();
      }

      // 画线交互: 在 drawMode 非选择时点击主图生成标注
      function handleAnnoClick(px, py) {
        if (!chart || !session.value) return;
        let di = null, y = null;
        try {
          const r = chart.convertFromPixel({ seriesIndex: 0 }, [px, py]);
          if (r && r.length >= 2) { di = Math.round(r[0]); y = r[1]; }
        } catch (e) {}
        const maxIdx = preBars.value.length + session.value.bars.length;
        if (di == null || y == null || di < 0 || di >= maxIdx) return;
        const mode = drawMode.value;
        if (mode === 'hline') {
          annos.value.push({ type: 'hline', x: di, y: y });
          consumeDraw();
        } else if (mode === 'rect') {
          if (annoAnchor.value) {
            annos.value.push({ type: 'rect', x1: annoAnchor.value.x, y1: annoAnchor.value.y, x2: di, y2: y });
            annoAnchor.value = null;
            consumeDraw();
          } else annoAnchor.value = { x: di, y: y };
        } else if (mode === 'line' || mode === 'ray' || mode === 'arrow') {
          if (annoAnchor.value) {
            annos.value.push({ type: mode, x1: annoAnchor.value.x, y1: annoAnchor.value.y, x2: di, y2: y });
            annoAnchor.value = null;
            consumeDraw();
          } else annoAnchor.value = { x: di, y: y };
        }
        syncAnnos();
        draw(true);
      }

      // 点击主图任意K线 → 右侧分时联动 (前置段与训练段都支持), 画线模式分流
      function bindChartClick() {
        if (!chart) return;
        const zr = chart.getZr();
        zr.off('click');
        zr.on('click', function (params) {
          if (drawMode.value !== 'select') {
            handleAnnoClick(params.offsetX, params.offsetY);
            return;
          }
          let di = null;
          try {
            const r = chart.convertFromPixel({ seriesIndex: 0 }, [params.offsetX, params.offsetY]);
            if (r && r.length) di = Math.round(r[0]);
          } catch (e) {}
          if (di == null && params.dataIndex != null) di = params.dataIndex;
          const maxIdx = preBars.value.length + (session.value ? session.value.bars.length : 0);
          if (di != null && di >= 0 && di < maxIdx) loadIntraday(di);
        });
      }

      onMounted(function () {
        loadCategories();
        loadStrategies();
        window.addEventListener('resize', function () {
          if (chart) chart.resize();
          if (dateOpen.value) computePos();
        });
        // 日历/搜索下拉: 点击外部关闭 (弹层为 fixed, 需排除触发块自身)
        document.addEventListener('mousedown', function (e) {
          if (!dateOpen.value && !codeOpen.value) return;
          const t = e.target;
          if (t && t.closest && (t.closest('.date-pop') || t.closest('.date-field') || t.closest('.code-search'))) return;
          dateOpen.value = false; codeOpen.value = false;
        });
        // 容器滚动时(日历弹层为 fixed) 重新定位, 保证紧跟触发块
        window.addEventListener('scroll', function () { if (dateOpen.value) computePos(); }, true);
        // 快捷键盘: → 右箭头 = 观望一步 (等同点击「观望」); ↑/↓ = 缩放滑动条范围; Esc 关闭浮层
        window.addEventListener('keydown', function (e) {
          if (e.key === 'Escape' && (dateOpen.value || codeOpen.value)) {
            dateOpen.value = false; codeOpen.value = false; codeResults.value = [];
            if (e.target && e.target.blur) e.target.blur();
            return;
          }
          if (!props.active) return;
          const tag = (e.target && e.target.tagName) || '';
          if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA' || e.target && e.target.isContentEditable) return;
          // 需求101506-4: ↑/↓ 缩放主图滑动条范围 (与训练是否进行中无关)
          if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
            e.preventDefault();
            if (chart) window.KZoom(chart, e.key === 'ArrowUp' ? 'in' : 'out');
            return;
          }
          if (!session.value || session.value.status !== 'active') return;
          if (e.key === 'ArrowRight') { e.preventDefault(); holdAndAdvance(); }
        });
        // 黑白主题切换 → 按新配色重绘当前训练图 (ECharts 需重新取值)
        window.addEventListener('themechange', function () {
          if (!session.value) return;
          draw(true);
          disposePre();
          showCurrentIntraday();
        });
        // 登录/登出/补绑手机号 → 立即刷新「可开始训练」状态 (无需切 Tab) + 重新预生成
        window.addEventListener('authchange', function () {
          refreshLogin();
          if (!session.value) schedulePrime();
        });
        if (props.active) schedulePrime();       // 需求101604: 首次挂载即预生成
      });

      // Tab 切回训练时: 隐藏期间容器尺寸为 0, 需要重新建图 / resize
      watch(function () { return props.active; }, async function (v) {
        refreshLogin();
        if (!v) return;
        loadCategories(true);                      // 需求101701-2: 每次进入训练模块刷新品类
        if (!session.value) { prime(); return; }   // 需求101604: 未开局 → 提前预生成本局数据
        await ensureChart();
        draw();
        disposePre();
        showCurrentIntraday();
      });

      // 需求101604: 开局参数变化 → 去抖后重新预生成 (保证点击时数据已备好)
      watch(function () {
        return [form.category, form.bars, form.code, form.start_date,
                form.initial_capital, form.leverage, form.strategy_id].join('|');
      }, function () { if (!session.value) schedulePrime(); });

      // 均线开关变化 → 保留视图重绘
      watch(maOn, function () { draw(true); });

      return {
        categories, strategies, session, loading, error, form, newStrategyName,
        loggedIn, notice,
        // 需求2: 指定标的搜索
        codeKw, codeResults, codeOpen, codeLabel, onCodeInput, onCodeFocus,
        pickCode, pickCodeFirst, closeCodeList, clearCodeKw,
        // 需求1+4: 自定义日历弹层
        availHint, dateOpen, dateField, datePopStyle, dowLabels, quickRanges,
        calY, calM, calCells, calTip, todayStr, toggleCalendar, closeCalendar,
        stepMonth, stepYear, pickDate, clearDate, quickPick,
        barsMax, barsStep,
        note, savedMsg, qty, positionRatio,
        condKind, condPrice, condTrail, condRatio, condNotice, activeConds, firedConds, skippedConds, setConditional, cancelConditional,
        progressPct, pnlClass, canBuy, sellLocked,
        trainPeriods,
        preBars, preCount, preDate, preAmp,
        maKeys, maOn, resetMa,
        drawMode, annos, annoAnchor, setDrawMode, clearAnnos,
        drawCount, annoLeft, drawDone, continueDraw,
        pickCategory, addStrategy, delStrategy, start, act, holdAndAdvance, doAdvance, advance5, switchPeriod,
        settle, restart,
        saveStrategy, saveNote, fmt,
        resetForm, clearCode,
        // 需求101802-1: 训练标的池
        poolOpen, poolRows, poolTotal, poolFilters, poolFilter, poolLoading,
        poolHasMore, poolPct, openPool, closePool, pickPoolFilter, poolMore, usePoolRow,
        // 需求101808-3: 标的池品类维度
        poolCategory, pickPoolCategory,
      };
    },
  };
})();

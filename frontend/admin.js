/**
 * 系统后台 Tab (需求101809) —— 账户管理 / 访问IP收集管理 / 安全管理(基础防护储备)
 *
 * 仅管理员角色可见该 Tab (app.js 按 user.role === 'admin' 动态追加)。
 * 全部数据来自 /api/admin/* 接口 (需管理员 token; 自身不受访问收集/封禁/限流影响)。
 *
 * 分区:
 *   概览   —— 账户统计 + 访问统计 + 近14天访问趋势 + 热点路径/Top IP
 *   账户   —— 用户列表(搜索/分页) + 禁用/启用 / 角色调整 / 重置密码 / 删除
 *   访问IP —— 访问明细(ip筛选/分页) 与 按 IP 聚合(访问次数/首末次/账号数)
 *   安全   —— 安全防护配置(速率限制等) + 封禁名单管理
 */
(function () {
  'use strict';
  const { ref, reactive, computed, watch } = Vue;

  async function api(path, opts) {
    return window.__apiFetch(path, opts);
  }
  function fmt(v) { return v == null || v === '' ? '--' : v; }
  function pctOf(a, b) { return b ? Math.round(a / b * 100) : 0; }

  window.AdminPane = {
    name: 'admin-pane',
    props: { active: { type: Boolean, default: false } },
    template: `
<div class="adm-wrap">
  <div class="adm-subtabs">
    <span v-for="t in subTabs" :key="t.id"
          :class="['adm-subtab', {active: sub === t.id}]"
          @click="sub = t.id">{{ t.icon }} {{ t.label }}</span>
    <span class="adm-spacer"></span>
    <button class="adm-btn ghost" @click="refreshAll" :disabled="loading">↻ 刷新</button>
  </div>

  <div v-if="err" class="adm-err">{{ err }}</div>

  <!-- ===== 概览 ===== -->
  <div v-show="sub === 'overview'" class="adm-body">
    <div class="adm-cards">
      <div class="adm-card"><b>{{ ov.users.total }}</b><span>注册用户</span></div>
      <div class="adm-card"><b class="ok">{{ ov.users.online }}</b><span>当前在线</span></div>
      <div class="adm-card"><b class="ok">{{ ov.users.new_today }}</b><span>今日新增</span></div>
      <div class="adm-card"><b :class="ov.users.banned ? 'warn' : ''">{{ ov.users.banned }}</b><span>已禁用</span></div>
      <div class="adm-card"><b>{{ ov.users.admins }}</b><span>管理员</span></div>
      <div class="adm-card"><b>{{ ov.visits.today }}</b><span>今日访问</span></div>
      <div class="adm-card"><b>{{ ov.visits.unique_ips_today }}</b><span>今日独立IP</span></div>
      <div class="adm-card"><b :class="ov.blocks ? 'warn' : ''">{{ ov.blocks }}</b><span>封禁IP</span></div>
    </div>

    <div class="adm-panel">
      <div class="adm-panel-head">近 14 天访问趋势 <i>共 {{ ov.visits.total }} 次 · 独立IP {{ ov.visits.unique_ips }}</i></div>
      <div class="adm-chart">
        <div v-for="d in ov.visits.daily" :key="d.date" class="adm-bar-col" :title="d.date + ' · ' + d.count + ' 次'">
          <div class="adm-bar" :style="{height: barH(d.count) + '%'}"></div>
          <span class="adm-bar-x">{{ d.date.slice(5) }}</span>
        </div>
        <div v-if="!ov.visits.daily.length" class="adm-empty">暂无访问数据</div>
      </div>
    </div>

    <div class="adm-two">
      <div class="adm-panel">
        <div class="adm-panel-head">热点路径 Top</div>
        <div class="adm-list">
          <div v-for="p in ov.visits.top_paths" :key="p.path" class="adm-list-row">
            <span class="adm-ellip">{{ p.path }}</span><b>{{ p.count }}</b>
          </div>
          <div v-if="!ov.visits.top_paths.length" class="adm-empty">暂无数据</div>
        </div>
      </div>
      <div class="adm-panel">
        <div class="adm-panel-head">活跃 IP Top</div>
        <div class="adm-list">
          <div v-for="p in ov.visits.top_ips" :key="p.ip" class="adm-list-row">
            <span class="adm-ellip">{{ p.ip }}</span>
            <span class="adm-row-ops">
              <b>{{ p.count }}</b>
              <button class="adm-mini danger" @click="quickBlock(p.ip)">封禁</button>
            </span>
          </div>
          <div v-if="!ov.visits.top_ips.length" class="adm-empty">暂无数据</div>
        </div>
      </div>
    </div>
  </div>

  <!-- ===== 账户管理 ===== -->
  <div v-show="sub === 'users'" class="adm-body">
    <div class="adm-toolbar">
      <input class="adm-input" v-model="uKw" placeholder="🔍 用户名 / 手机号 / 邮箱 / ID" @keyup.enter="loadUsers(0)">
      <button class="adm-btn" @click="loadUsers(0)">搜索</button>
      <span class="adm-spacer"></span>
      <span class="adm-count">共 {{ uTotal }} 个账户</span>
    </div>
    <div class="adm-table-wrap">
      <table class="adm-table">
        <thead><tr>
          <th>用户名</th><th>手机号</th><th>邮箱</th><th>角色</th><th>状态</th>
          <th>注册时间</th><th>最近登录</th><th>操作</th>
        </tr></thead>
        <tbody>
          <tr v-for="u in users" :key="u.user_id">
            <td class="adm-strong">{{ u.username }}<i class="adm-uid">{{ u.user_id.slice(0,8) }}</i></td>
            <td>{{ fmt(u.phone) }}</td>
            <td>{{ fmt(u.email) }}</td>
            <td><span :class="['adm-tag', u.role === 'admin' ? 'admin' : '']">{{ u.role === 'admin' ? '管理员' : '用户' }}</span></td>
            <td><span :class="['adm-tag', u.status === 'active' ? 'ok' : 'bad']">{{ u.status === 'active' ? '正常' : '已禁用' }}</span></td>
            <td class="adm-dim">{{ fmt(u.created_at) }}</td>
            <td class="adm-dim">{{ fmt(u.last_login_at) }}</td>
            <td class="adm-ops">
              <button class="adm-mini" v-if="u.role !== 'admin'"
                      @click="setStatus(u, u.status === 'active' ? 'banned' : 'active')">
                {{ u.status === 'active' ? '禁用' : '启用' }}
              </button>
              <button class="adm-mini" v-if="u.role !== 'admin'" @click="setRole(u, 'admin')">设为管理员</button>
              <button class="adm-mini" v-else @click="setRole(u, 'user')">取消管理员</button>
              <button class="adm-mini" @click="resetPwd(u)">重置密码</button>
              <button class="adm-mini danger" v-if="u.role !== 'admin'" @click="delUser(u)">删除</button>
            </td>
          </tr>
          <tr v-if="!users.length"><td colspan="8" class="adm-empty">暂无账户</td></tr>
        </tbody>
      </table>
    </div>
    <div class="adm-pager" v-if="uTotal > uPageSize">
      <button class="adm-btn ghost" :disabled="uOffset <= 0" @click="loadUsers(uOffset - uPageSize)">‹ 上一页</button>
      <span>{{ uOffset + 1 }} - {{ Math.min(uOffset + uPageSize, uTotal) }} / {{ uTotal }}</span>
      <button class="adm-btn ghost" :disabled="uOffset + uPageSize >= uTotal" @click="loadUsers(uOffset + uPageSize)">下一页 ›</button>
    </div>
  </div>

  <!-- ===== 访问IP ===== -->
  <div v-show="sub === 'ips'" class="adm-body">
    <div class="adm-subsubtabs">
      <span :class="['adm-subsubtab', {active: ipMode === 'agg'}]" @click="ipMode = 'agg'; loadIps(0)">按 IP 聚合</span>
      <span :class="['adm-subsubtab', {active: ipMode === 'detail'}]" @click="ipMode = 'detail'; loadVisits(0)">访问明细</span>
      <span class="adm-spacer"></span>
      <button class="adm-btn ghost" @click="clearVisits">清空历史日志</button>
    </div>

    <template v-if="ipMode === 'agg'">
      <div class="adm-toolbar">
        <span class="adm-count">共 {{ ipTotal }} 个独立 IP</span>
        <span class="adm-spacer"></span>
        <button class="adm-btn ghost" :class="{on: ipOrder === 'count'}" @click="ipOrder = 'count'; loadIps(0)">按次数</button>
        <button class="adm-btn ghost" :class="{on: ipOrder === 'recent'}" @click="ipOrder = 'recent'; loadIps(0)">按最近</button>
      </div>
      <div class="adm-table-wrap">
        <table class="adm-table">
          <thead><tr><th>IP</th><th>访问次数</th><th>首次访问</th><th>最近访问</th><th>账号数</th><th>状态</th><th>操作</th></tr></thead>
          <tbody>
            <tr v-for="r in ips" :key="r.ip">
              <td class="adm-strong">{{ r.ip }}</td>
              <td>{{ r.count }}</td>
              <td class="adm-dim">{{ r.first_seen }}</td>
              <td class="adm-dim">{{ r.last_seen }}</td>
              <td>{{ r.users }}</td>
              <td><span :class="['adm-tag', r.blocked ? 'bad' : 'ok']">{{ r.blocked ? '已封禁' : '正常' }}</span></td>
              <td class="adm-ops">
                <button v-if="!r.blocked" class="adm-mini danger" @click="quickBlock(r.ip)">封禁</button>
                <button v-else class="adm-mini" @click="unblock(r.ip)">解封</button>
              </td>
            </tr>
            <tr v-if="!ips.length"><td colspan="7" class="adm-empty">暂无访问记录</td></tr>
          </tbody>
        </table>
      </div>
      <div class="adm-pager" v-if="ipTotal > ipPageSize">
        <button class="adm-btn ghost" :disabled="ipOffset <= 0" @click="loadIps(ipOffset - ipPageSize)">‹ 上一页</button>
        <span>{{ ipOffset + 1 }} - {{ Math.min(ipOffset + ipPageSize, ipTotal) }} / {{ ipTotal }}</span>
        <button class="adm-btn ghost" :disabled="ipOffset + ipPageSize >= ipTotal" @click="loadIps(ipOffset + ipPageSize)">下一页 ›</button>
      </div>
    </template>

    <template v-else>
      <div class="adm-toolbar">
        <input class="adm-input" v-model="vIp" placeholder="🔍 按 IP 筛选" @keyup.enter="loadVisits(0)">
        <button class="adm-btn" @click="loadVisits(0)">搜索</button>
        <span class="adm-spacer"></span>
        <span class="adm-count">共 {{ vTotal }} 条记录</span>
      </div>
      <div class="adm-table-wrap">
        <table class="adm-table">
          <thead><tr><th>时间</th><th>IP</th><th>方法</th><th>路径</th><th>账号</th><th>状态</th><th>UA</th></tr></thead>
          <tbody>
            <tr v-for="r in visits" :key="r.id">
              <td class="adm-dim">{{ r.ts }}</td>
              <td class="adm-strong">{{ r.ip }}</td>
              <td>{{ r.method }}</td>
              <td class="adm-ellip">{{ r.path }}</td>
              <td class="adm-dim">{{ r.user_id || '--' }}</td>
              <td><span :class="['adm-tag', r.status >= 400 ? 'bad' : 'ok']">{{ r.status }}</span></td>
              <td class="adm-dim adm-ellip">{{ r.ua }}</td>
            </tr>
            <tr v-if="!visits.length"><td colspan="7" class="adm-empty">暂无访问记录</td></tr>
          </tbody>
        </table>
      </div>
      <div class="adm-pager" v-if="vTotal > vPageSize">
        <button class="adm-btn ghost" :disabled="vOffset <= 0" @click="loadVisits(vOffset - vPageSize)">‹ 上一页</button>
        <span>{{ vOffset + 1 }} - {{ Math.min(vOffset + vPageSize, vTotal) }} / {{ vTotal }}</span>
        <button class="adm-btn ghost" :disabled="vOffset + vPageSize >= vTotal" @click="loadVisits(vOffset + vPageSize)">下一页 ›</button>
      </div>
    </template>
  </div>

  <!-- ===== 安全管理 ===== -->
  <div v-show="sub === 'security'" class="adm-body">
    <div class="adm-panel">
      <div class="adm-panel-head">安全防护配置 <i>基础防护储备 · 默认不改变现有行为</i></div>
      <div class="adm-form">
        <label class="adm-field">
          <span>访问日志记录</span>
          <select class="adm-input" v-model="sec.record_visits">
            <option value="1">开启（记录访问IP）</option>
            <option value="0">关闭</option>
          </select>
        </label>
        <label class="adm-field">
          <span>速率限制</span>
          <select class="adm-input" v-model="sec.rate_limit_enabled">
            <option value="0">关闭</option>
            <option value="1">开启</option>
          </select>
        </label>
        <label class="adm-field">
          <span>每 IP 每分钟上限</span>
          <input class="adm-input" type="number" min="1" v-model="sec.rate_limit_per_min">
        </label>
        <label class="adm-field">
          <span>手动封禁默认时长(小时)</span>
          <input class="adm-input" type="number" min="0" v-model="sec.ip_block_hours">
          <i class="adm-hint">0 = 永久封禁</i>
        </label>
        <div class="adm-form-foot">
          <button class="adm-btn" @click="saveSecurity" :disabled="loading">保存配置</button>
          <span v-if="secMsg" class="adm-msg">{{ secMsg }}</span>
        </div>
      </div>
    </div>

    <div class="adm-panel">
      <div class="adm-panel-head">封禁名单 <i>{{ blocks.length }} 个</i></div>
      <div class="adm-toolbar">
        <input class="adm-input" v-model="blkIp" placeholder="输入要封禁的 IP">
        <input class="adm-input adm-input-sm" v-model="blkReason" placeholder="原因(可选)">
        <input class="adm-input adm-input-xs" type="number" min="0" v-model="blkHours" placeholder="时长h">
        <button class="adm-btn danger" @click="doBlock">封禁</button>
      </div>
      <div class="adm-list">
        <div v-for="b in blocks" :key="b.ip" class="adm-list-row">
          <span class="adm-strong">{{ b.ip }}</span>
          <span class="adm-ellip adm-dim">{{ b.reason || '无原因' }} · {{ b.expires_at ? '至 ' + b.expires_at : '永久' }}</span>
          <span class="adm-row-ops"><button class="adm-mini" @click="unblock(b.ip)">解封</button></span>
        </div>
        <div v-if="!blocks.length" class="adm-empty">当前无封禁记录</div>
      </div>
    </div>
  </div>
</div>`,
    setup(props) {
      const subTabs = [
        { id: 'overview', label: '概览', icon: '📊' },
        { id: 'users', label: '账户管理', icon: '👤' },
        { id: 'ips', label: '访问IP', icon: '🌐' },
        { id: 'security', label: '安全管理', icon: '🛡' },
      ];
      const sub = ref('overview');
      const loading = ref(false);
      const err = ref('');
      const ov = reactive({ users: { total: 0, online: 0, new_today: 0, banned: 0, admins: 0 },
                            visits: { total: 0, today: 0, unique_ips: 0, unique_ips_today: 0,
                                      top_paths: [], top_ips: [], daily: [] },
                            blocks: 0 });
      const maxDaily = computed(() => Math.max(1, ...ov.visits.daily.map(d => d.count)));
      function barH(c) { return Math.round(c / maxDaily.value * 100); }

      // 账户
      const users = ref([]); const uKw = ref(''); const uTotal = ref(0);
      const uOffset = ref(0); const uPageSize = 20;
      // 访问IP
      const ipMode = ref('agg'); const ips = ref([]); const ipTotal = ref(0);
      const ipOffset = ref(0); const ipPageSize = 20; const ipOrder = ref('count');
      const visits = ref([]); const vTotal = ref(0); const vOffset = ref(0);
      const vPageSize = 20; const vIp = ref('');
      // 安全
      const sec = reactive({ record_visits: '1', rate_limit_enabled: '0',
                             rate_limit_per_min: '120', ip_block_hours: '24',
                             auto_block_max_per_day: '0' });
      const secMsg = ref(''); const blocks = ref([]);
      const blkIp = ref(''); const blkReason = ref(''); const blkHours = ref('');

      function guard(fn) {
        return async (...a) => {
          err.value = '';
          try { await fn(...a); }
          catch (e) {
            let m = e.message || String(e);
            try { m = JSON.parse(m).detail || JSON.parse(m).reason || m; } catch (x) {}
            err.value = '❌ ' + String(m).replace(/^"|"$/g, '');
          }
        };
      }

      const loadOverview = guard(async () => {
        const d = await api('/admin/overview');
        ov.users = d.users || ov.users;
        ov.visits = d.visits || ov.visits;
        ov.blocks = d.blocks || 0;
      });
      const loadUsers = guard(async (off) => {
        uOffset.value = Math.max(0, off || 0);
        const d = await api(`/admin/users?keyword=${encodeURIComponent(uKw.value)}&limit=${uPageSize}&offset=${uOffset.value}`);
        users.value = d.users || []; uTotal.value = d.total || 0;
      });
      const loadIps = guard(async (off) => {
        ipOffset.value = Math.max(0, off || 0);
        const d = await api(`/admin/ips?limit=${ipPageSize}&offset=${ipOffset.value}&order=${ipOrder.value}`);
        ips.value = d.ips || []; ipTotal.value = d.total || 0;
      });
      const loadVisits = guard(async (off) => {
        vOffset.value = Math.max(0, off || 0);
        const d = await api(`/admin/visits?limit=${vPageSize}&offset=${vOffset.value}&ip=${encodeURIComponent(vIp.value)}`);
        visits.value = d.visits || []; vTotal.value = d.total || 0;
      });
      const loadSecurity = guard(async () => {
        const d = await api('/admin/security');
        Object.assign(sec, d || {});
        await loadBlocks();
      });
      const loadBlocks = guard(async () => {
        const d = await api('/admin/blocks');
        blocks.value = d.blocks || [];
      });

      // 账户操作
      const setStatus = guard(async (u, status) => {
        if (status === 'banned' && !confirm(`确认禁用账号「${u.username}」? 其所有登录会话将立即失效。`)) return;
        await api(`/admin/users/${u.user_id}/status`, { method: 'POST', body: JSON.stringify({ status }) });
        await loadUsers(uOffset.value);
      });
      const setRole = guard(async (u, role) => {
        if (role === 'user' && !confirm(`确认取消「${u.username}」的管理员权限?`)) return;
        await api(`/admin/users/${u.user_id}/role`, { method: 'POST', body: JSON.stringify({ role }) });
        await loadUsers(uOffset.value);
      });
      const resetPwd = guard(async (u) => {
        const pw = prompt(`为「${u.username}」设置新密码 (8-64位, 含大小写/数字/特殊字符至少三类):`, '');
        if (pw == null) return;
        await api(`/admin/users/${u.user_id}/reset-password`, { method: 'POST', body: JSON.stringify({ new_password: pw }) });
        alert('密码已重置, 该用户需重新登录。');
      });
      const delUser = guard(async (u) => {
        if (!confirm(`确认删除账号「${u.username}」? 该账号的会话与数据将一并清除, 不可恢复。`)) return;
        await api(`/admin/users/${u.user_id}`, { method: 'DELETE' });
        await loadUsers(uOffset.value);
      });

      // IP 封禁
      const quickBlock = guard(async (ip) => {
        const h = parseInt(sec.ip_block_hours || '24', 10);
        if (!confirm(`确认封禁 IP ${ip}? (${h > 0 ? h + ' 小时' : '永久'})`)) return;
        await api('/admin/blocks', { method: 'POST', body: JSON.stringify({ ip, reason: '手动封禁', hours: h }) });
        await Promise.all([ipMode.value === 'agg' ? loadIps(ipOffset.value) : loadVisits(vOffset.value), loadBlocks(), loadOverview()]);
      });
      const doBlock = guard(async () => {
        if (!blkIp.value.trim()) { err.value = '请输入要封禁的 IP'; return; }
        await api('/admin/blocks', { method: 'POST', body: JSON.stringify({
          ip: blkIp.value.trim(), reason: blkReason.value.trim(),
          hours: parseInt(blkHours.value || '0', 10) || 0 }) });
        blkIp.value = ''; blkReason.value = ''; blkHours.value = '';
        await loadBlocks();
      });
      const unblock = guard(async (ip) => {
        await api(`/admin/blocks/${encodeURIComponent(ip)}`, { method: 'DELETE' });
        await Promise.all([loadBlocks(), loadIps(ipOffset.value), loadOverview()]);
      });
      const clearVisits = guard(async () => {
        if (!confirm('确认清空全部历史访问日志? 该操作不可恢复。')) return;
        await api('/admin/visits?days=0', { method: 'DELETE' });
        await Promise.all([loadVisits(0), loadIps(0), loadOverview()]);
      });
      const saveSecurity = guard(async () => {
        secMsg.value = '';
        const d = await api('/admin/security', { method: 'POST', body: JSON.stringify({ settings: { ...sec } }) });
        Object.assign(sec, d || {});
        secMsg.value = '✅ 已保存';
        setTimeout(() => { secMsg.value = ''; }, 2000);
      });

      const refreshAll = guard(async () => {
        loading.value = true;
        try {
          await loadOverview();
          if (sub.value === 'users') await loadUsers(uOffset.value);
          else if (sub.value === 'ips') await (ipMode.value === 'agg' ? loadIps(ipOffset.value) : loadVisits(vOffset.value));
          else if (sub.value === 'security') await loadSecurity();
        } finally { loading.value = false; }
      });

      // 进入后台 Tab 或切换子页时按需加载
      watch(() => props.active, async (on) => {
        if (on && window.localStorage.getItem('auth_token')) await loadOverview();
      });
      watch(sub, async (s) => {
        if (s === 'users') await loadUsers(0);
        else if (s === 'ips') await (ipMode.value === 'agg' ? loadIps(0) : loadVisits(0));
        else if (s === 'security') await loadSecurity();
        else await loadOverview();
      });
      watch(ipMode, async (m) => { await (m === 'agg' ? loadIps(0) : loadVisits(0)); });

      if (props.active) loadOverview();

      return {
        subTabs, sub, loading, err, ov, barH,
        users, uKw, uTotal, uOffset, uPageSize,
        ipMode, ips, ipTotal, ipOffset, ipPageSize, ipOrder,
        visits, vTotal, vOffset, vPageSize, vIp,
        sec, secMsg, blocks, blkIp, blkReason, blkHours,
        loadOverview, loadUsers, loadIps, loadVisits, loadSecurity, loadBlocks,
        setStatus, setRole, resetPwd, delUser,
        quickBlock, doBlock, unblock, clearVisits, saveSecurity, refreshAll,
        fmt, pctOf,
      };
    },
  };
})();

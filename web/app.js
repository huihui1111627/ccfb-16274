/* 级联故障推演 —— 前端 */
'use strict';

const $ = (sel, root) => (root || document).querySelector(sel);
const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

const state = {
  overview: null,
  topo: null,
  focus: null,
  selectedEdge: null,
  selectedService: null,
  tab: 'config',
  sim: null,          // 当前推演结果
  frames: [],
  frameIdx: 0,
  playing: false,
  playTimer: null,
  viewMode: 'incident',
  domain: '',
  nodePos: new Map(),
  incidentServices: new Set(),
};

// ---------------------------------------------------------------- helpers
function fmtNum(n, d = 1) {
  if (n === null || n === undefined || Number.isNaN(n)) return '–';
  if (Math.abs(n) >= 1000) return Math.round(n).toLocaleString();
  return Number(n).toFixed(d).replace(/\.0+$/, '');
}
function fmtMs(n) {
  if (n === null || n === undefined) return '–';
  if (n >= 1000) return fmtNum(n / 1000, 2) + 's';
  return fmtNum(n, 0) + 'ms';
}
function pct(n) { return (n * 100).toFixed(1) + '%'; }
function esc(s) { return String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }

async function api(path, opts) {
  setStatus('请求中…');
  try {
    const r = await fetch(path, opts);
    if (!r.ok) {
      const t = await r.json().catch(() => ({}));
      throw new Error(t.error || ('HTTP ' + r.status));
    }
    setStatus('');
    return await r.json();
  } catch (e) {
    setStatus('错误：' + e.message, true);
    throw e;
  }
}
function setStatus(msg, isErr) {
  const el = $('#status');
  el.textContent = msg;
  el.style.color = isErr ? 'var(--bad)' : 'var(--muted)';
}

// ---------------------------------------------------------------- init
async function init() {
  bindUI();
  const [ov] = await Promise.all([api('/api/overview')]);
  state.overview = ov;
  renderHeader(ov);
  fillDomainFilter(ov);
  const params = new URLSearchParams();
  params.set('mode', state.viewMode);
  const [topo, allTopo] = await Promise.all([
    api('/api/topology?' + params.toString()),
    api('/api/topology?mode=all&max_nodes=2000'),
  ]);
  state.topo = topo;
  buildFaultServiceList(allTopo);
  layoutGraph(topo);
  renderGraph();
  const hint = $('#canvas-hint');
  if (hint) hint.textContent = `显示 ${topo.shown} / ${topo.total} 个服务 · 点击服务节点选择故障点 · 点击边查看三来源配置`;
  openTab('config');
}

function renderHeader(ov) {
  $('#company-name').textContent = ov.company || '模拟公司';
  $('#stat-services').textContent = ov.n_services;
  $('#stat-edges').textContent = ov.n_edges;
  $('#stat-inverted').textContent = ov.totals.n_inverted_edges;
  $('#stat-amp').textContent = '×' + ov.totals.max_leaf_amplification;
  if (ov.incident && ov.incident.chain) {
    state.incidentServices = new Set(ov.incident.chain);
  }
}

function fillDomainFilter(ov) {
  const sel = $('#domain-filter');
  ov.domains.forEach(d => {
    const o = document.createElement('option');
    o.value = d.domain; o.textContent = `${d.domain} (${d.count})`;
    sel.appendChild(o);
  });
}

function buildFaultServiceList(topo) {
  const sel = $('#fault-service');
  sel.innerHTML = '';
  const nodes = [...topo.nodes].sort((a, b) => (b.incident - a.incident) || a.name.localeCompare(b.name));
  nodes.forEach(n => {
    const o = document.createElement('option');
    o.value = n.id;
    o.textContent = (n.incident ? '★ ' : '') + n.name + '  · ' + n.domain;
    if (n.id === 'inventory-svc-01') o.selected = true;
    sel.appendChild(o);
  });
  if (!nodes.find(n => n.id === sel.value) && nodes.length) sel.value = nodes[0].id;
}

// ---------------------------------------------------------------- UI bindings
function bindUI() {
  $('#view-mode').addEventListener('change', async () => {
    state.viewMode = $('#view-mode').value;
    await reloadTopology();
  });
  $('#domain-filter').addEventListener('change', async () => {
    state.domain = $('#domain-filter').value;
    await reloadTopology();
  });
  $('#chk-inverted').addEventListener('change', () => renderGraph());
  $('#chk-labels').addEventListener('change', () => renderGraph());
  $('#btn-simulate').addEventListener('click', runSimulation);
  $('#btn-reset-view').addEventListener('click', async () => {
    await reloadTopology();
  });
  $('#btn-incident').addEventListener('click', reproduceIncident);

  $('#fault-service').addEventListener('change', () => renderGraph());

  $$('.tabs button').forEach(b => b.addEventListener('click', () => openTab(b.dataset.tab)));

  $('#btn-play').addEventListener('click', togglePlay);
  $('#btn-step-fwd').addEventListener('click', () => { pause(); stepFrame(1); });
  $('#btn-step-back').addEventListener('click', () => { pause(); stepFrame(-1); });
  $('#tl-range').addEventListener('input', e => {
    pause();
    state.frameIdx = parseInt(e.target.value, 10) || 0;
    applyFrame();
  });
  $('#play-speed').addEventListener('change', () => { if (state.playing) { pause(); play(); } });

  // drag pan / wheel zoom on svg (simple)
  const svg = $('#graph');
  let dragging = false, last = null;
  svg.addEventListener('mousedown', e => {
    if (e.target.closest('.node') || e.target.closest('.edge-hit')) return;
    dragging = true; last = { x: e.clientX, y: e.clientY };
  });
  window.addEventListener('mousemove', e => {
    if (!dragging) return;
    const dx = e.clientX - last.x, dy = e.clientY - last.y;
    last = { x: e.clientX, y: e.clientY };
    state.pan.x += dx; state.pan.y += dy;
    applyTransform();
  });
  window.addEventListener('mouseup', () => { dragging = false; });
  svg.addEventListener('wheel', e => {
    e.preventDefault();
    const k = e.deltaY < 0 ? 1.12 : 1 / 1.12;
    state.zoom = Math.max(0.15, Math.min(4, state.zoom * k));
    applyTransform();
  }, { passive: false });
}

async function reloadTopology() {
  const params = new URLSearchParams();
  if (state.domain) params.set('domain', state.domain);
  params.set('mode', state.viewMode);
  const topo = await api('/api/topology?' + params.toString());
  state.topo = topo;
  layoutGraph(state.topo);
  renderGraph();
  const hint = $('#canvas-hint');
  if (hint) hint.textContent = `显示 ${topo.shown} / ${topo.total} 个服务` +
    (topo.truncated ? '（已按问题严重度精简）' : '') +
    ' · 点击服务节点选择故障点 · 点击边查看三来源配置';
}

function filterIncidentNeighborhood(topo) {
  // 已由后端 mode=incident 处理；这里只保留以防旧数据
  return topo;
}

// ---------------------------------------------------------------- graph layout
function layoutGraph(topo) {
  state.zoom = 1; state.pan = { x: 0, y: 0 };
  const byTier = new Map();
  topo.nodes.forEach(n => {
    const t = n.tier || 0;
    if (!byTier.has(t)) byTier.set(t, []);
    byTier.get(t).push(n);
  });
  const tiers = [...byTier.keys()].sort((a, b) => a - b);
  const pos = new Map();

  // 每个 tier 一行，行内按列数换行成网格；事故节点排到本行中间
  const cols = Math.max(1, Math.ceil(Math.sqrt(topo.nodes.length * 1.6)));
  const colW = 150, rowH = 96;
  const inset = 80;

  // 先估算每个 tier 需要的行数
  const tierRows = tiers.map(t => {
    const arr = byTier.get(t);
    const rows = Math.ceil(arr.length / cols);
    return { tier: t, rows };
  });
  let totalRows = tierRows.reduce((a, x) => a + x.rows, 0);
  const W = inset * 2 + Math.max(1, cols) * colW;
  const H = inset * 2 + totalRows * rowH;

  let y = inset;
  tierRows.forEach(({ tier, rows }) => {
    const arr = byTier.get(tier);
    arr.sort((a, b) => (b.incident - a.incident) || a.id.localeCompare(b.id));
    const tierH = rows * rowH;
    const spanX = W - inset * 2;
    arr.forEach((node, i) => {
      const r = Math.floor(i / cols);
      const c = i % cols;
      const inRow = Math.min(cols, arr.length - r * cols);
      const xx = inset + spanX * ((c + 0.5) / Math.max(1, inRow));
      const yy = y + (r + 0.5) * rowH;
      pos.set(node.id, { x: xx, y: yy });
    });
    y += tierH;
  });
  state.nodePos = pos;
  state.canvasSize = { W: Math.max(W, 900), H: Math.max(H, 560) };
}

function applyTransform() {
  const g = $('#g-root');
  if (!g) return;
  g.setAttribute('transform', `translate(${state.pan.x},${state.pan.y}) scale(${state.zoom})`);
}

function renderGraph() {
  const svg = $('#graph');
  const wrap = $('#canvas-wrap');
  const rect = wrap.getBoundingClientRect();
  const W = Math.max(rect.width, 400), H = Math.max(rect.height, 300);
  const topo = state.topo;
  if (!topo) return;

  // fit layout to viewport
  const base = state.canvasSize || { W: 1200, H: 800 };
  const scale = Math.min(W / base.W, H / base.H) * 0.98;
  state.baseScale = scale;
  const ox = (W - base.W * scale) / 2;
  const oy = (H - base.H * scale) / 2;

  const showLabels = $('#chk-labels').checked;
  const showInverted = $('#chk-inverted').checked;
  const faultSvc = $('#fault-service').value;

  const pos = state.nodePos;
  const nodesById = new Map(topo.nodes.map(n => [n.id, n]));
  const sim = state.sim;
  const curFrame = sim && state.frames.length ? state.frames[state.frameIdx] : null;

  let defs = `<defs>
    <marker id="arrow" viewBox="0 0 10 10" refX="22" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
      <path d="M 0 0 L 10 5 L 0 10 z" fill="#3f4a5a"/>
    </marker>
    <marker id="arrow-warn" viewBox="0 0 10 10" refX="22" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
      <path d="M 0 0 L 10 5 L 0 10 z" fill="#d29922"/>
    </marker>
    <marker id="arrow-bad" viewBox="0 0 10 10" refX="22" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
      <path d="M 0 0 L 10 5 L 0 10 z" fill="#f85149"/>
    </marker>
    <marker id="arrow-inc" viewBox="0 0 10 10" refX="22" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
      <path d="M 0 0 L 10 5 L 0 10 z" fill="#f778ba"/>
    </marker>
  </defs>`;

  let parts = [`<g id="g-root">`];
  parts.push(defs);

  // edges
  const edgeEls = [];
  const denseEdges = topo.edges.length > 80;
  topo.edges.forEach(e => {
    const a = pos.get(e.source), b = pos.get(e.target);
    if (!a || !b) return;
    const isIncident = e.incident;
    let color = '#3f4a5a';
    let marker = 'arrow';
    if (isIncident) { color = '#f778ba'; marker = 'arrow-inc'; }
    if (e.attempts > 1) { color = '#d29922'; marker = 'arrow-warn'; }
    if (showInverted && e.inverted) { color = '#f85149'; marker = 'arrow-bad'; }
    const selected = state.selectedEdge === e.id;

    // curve
    const dx = b.x - a.x, dy = b.y - a.y;
    const mx = (a.x + b.x) / 2 + (dy * 0.08), my = (a.y + b.y) / 2 - (dx * 0.08);
    const path = `M ${a.x} ${a.y} Q ${mx} ${my} ${b.x} ${b.y}`;
    const sw = selected ? 3.5 : (isIncident ? 2.4 : (e.inverted && showInverted ? 2 : 1.4));
    const dash = (e.inverted && showInverted) ? ' class="edge-line inverted"' : ' class="edge-line"';

    // per-frame animation: thickness by window attempts when playing
    let extra = '';
    if (curFrame && curFrame.edges && curFrame.edges[e.id]) {
      const win = curFrame.edges[e.id].window || 0;
      const tos = curFrame.edges[e.id].timeout_window || 0;
      if (win > 0 || tos > 0) {
        const glow = Math.min(1, (win + tos * 2) / 20);
        extra = ` data-win="${win}" data-tos="${tos}"`;
        color = mix(color, '#4c9aff', glow * 0.6);
      }
    }

    const showThisLabel = showLabels && (!denseEdges || isIncident || e.inverted);
    edgeEls.push(`<g class="edge" data-id="${esc(e.id)}"${extra}>
      <path class="edge-hit" d="${path}"></path>
      <path${dash} d="${path}" stroke="${color}" stroke-width="${sw}" marker-end="url(#${marker})"></path>
      ${showThisLabel ? `<text class="edge-label" x="${mx}" y="${my - 3}">${e.attempts > 1 ? '×' + e.attempts : ''}${e.inverted && showInverted ? ' ⚠' : ''}</text>` : ''}
    </g>`);
  });
  parts.push(`<g id="edges">${edgeEls.join('')}</g>`);

  // nodes
  const nodeEls = [];
  const dense = topo.nodes.length > 60;
  topo.nodes.forEach(n => {
    const p = pos.get(n.id);
    if (!p) return;
    const isRoot = n.root;
    const isIncident = n.incident;
    const isFault = n.id === faultSvc;
    let fill = '#1c2330';
    let stroke = isIncident ? '#f778ba' : (isRoot ? '#3fb950' : '#3a4556');
    if (isFault) { stroke = '#d29922'; fill = '#2a2416'; }
    const r = isIncident ? 13 : (isRoot ? 11 : 8);

    let busy = 0, queue = 0, sat = false;
    if (curFrame && curFrame.services && curFrame.services[n.id]) {
      const s = curFrame.services[n.id];
      busy = s.busy; queue = s.queue;
      sat = s.workers && queue >= Math.max(8, s.workers * 4);
    }
    const cls = ['node'];
    if (isRoot) cls.push('root');
    if (isIncident) cls.push('incident');
    if (isFault) cls.push('fault');

    let util = 0;
    if (curFrame && curFrame.services[n.id]) {
      const s = curFrame.services[n.id];
      util = s.workers ? s.busy / s.workers : 0;
    }
    const ring = util > 0 ? `<circle r="${r + 3}" fill="none" stroke="${util > .95 ? '#f85149' : '#4c9aff'}" stroke-width="2" opacity="${Math.min(1, util + .15)}"></circle>` : '';
    const pulse = sat ? `<circle class="pulse" r="8"></circle>` : '';

    // 密集时只给事故/入口/故障节点显示常驻标签，其余靠 hover
    const showLabel = !dense || isIncident || isRoot || isFault;
    const label = showLabel ? `<text x="0" y="${r + 12}" text-anchor="middle">${esc(n.name)}</text>` : '';

    nodeEls.push(`<g class="${cls.join(' ')}" data-id="${esc(n.id)}" transform="translate(${p.x},${p.y})">
      ${pulse}
      <circle class="body" r="${r}" fill="${fill}" stroke="${stroke}"></circle>
      ${ring}
      ${label}
    </g>`);
  });
  parts.push(`<g id="nodes">${nodeEls.join('')}</g>`);
  parts.push(`</g>`);
  svg.innerHTML = parts.join('');
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  state.pan = { x: ox, y: oy };
  state.zoom = scale;
  applyTransform();

  // events
  $$('.edge', svg).forEach(g => {
    g.addEventListener('click', ev => {
      ev.stopPropagation();
      selectEdge(g.dataset.id);
    });
    g.addEventListener('mousemove', ev => showEdgeTooltip(ev, g.dataset.id));
    g.addEventListener('mouseleave', hideTooltip);
  });
  $$('.node', svg).forEach(g => {
    g.addEventListener('click', ev => {
      ev.stopPropagation();
      selectService(g.dataset.id);
    });
    g.addEventListener('mousemove', ev => showNodeTooltip(ev, g.dataset.id));
    g.addEventListener('mouseleave', hideTooltip);
  });
  svg.classList.toggle('pulse', !!(sim && state.playing));
}

function mix(c1, c2, t) {
  const p = h => [parseInt(h.slice(1, 3), 16), parseInt(h.slice(3, 5), 16), parseInt(h.slice(5, 7), 16)];
  const a = p(c1), b = p(c2);
  const r = a.map((v, i) => Math.round(v + (b[i] - v) * t));
  return '#' + r.map(v => v.toString(16).padStart(2, '0')).join('');
}

// ---------------------------------------------------------------- tooltips
function showEdgeTooltip(ev, id) {
  const e = state.topo.edges.find(x => x.id === id);
  if (!e) return;
  const t = $('#tooltip');
  t.style.display = 'block';
  t.innerHTML = `<b>${esc(e.source)} → ${esc(e.target)}</b>
    <div class="kv">
      <div class="k">单次超时</div><div class="v">${fmtMs(e.timeout_ms)}</div>
      <div class="k">尝试次数</div><div class="v">${e.attempts}</div>
      <div class="k">最坏耗时</div><div class="v">${fmtMs(e.worst_case_ms)}</div>
      <div class="k">倒挂</div><div class="v">${e.inverted ? '<span class="bad">是</span>，上游放弃后下游还跑 ' + fmtMs(e.orphan_ms) : '否'}</div>
      <div class="k">超时传递</div><div class="v">${e.propagate_deadline ? '开' : '关'}</div>
      ${e.retry_budget ? `<div class="k">重试预算</div><div class="v">${pct(e.retry_budget)}</div>` : ''}
    </div>`;
  positionTooltip(ev);
}
function showNodeTooltip(ev, id) {
  const n = state.topo.nodes.find(x => x.id === id);
  if (!n) return;
  const t = $('#tooltip');
  t.style.display = 'block';
  t.innerHTML = `<b>${esc(n.name)}</b> <span class="muted">${esc(n.domain)}</span>
    <div class="kv">
      <div class="k">层级</div><div class="v">tier ${n.tier}</div>
      <div class="k">worker</div><div class="v">${n.workers}</div>
      <div class="k">处理耗时</div><div class="v">${fmtMs(n.service_ms)}</div>
      ${n.slo_ms ? `<div class="k">入口 SLO</div><div class="v">${fmtMs(n.slo_ms)}</div>` : ''}
      ${n.incident ? `<div class="k">标记</div><div class="v"><span class="pill incident">事故链</span></div>` : ''}
    </div>`;
  positionTooltip(ev);
}
function positionTooltip(ev) {
  const t = $('#tooltip');
  const wrap = $('#canvas-wrap').getBoundingClientRect();
  let x = ev.clientX - wrap.left + 14, y = ev.clientY - wrap.top + 14;
  if (x + 340 > wrap.width) x = ev.clientX - wrap.left - 350;
  t.style.left = Math.max(6, x) + 'px';
  t.style.top = Math.max(6, y) + 'px';
}
function hideTooltip() { $('#tooltip').style.display = 'none'; }

// ---------------------------------------------------------------- interactions
function selectEdge(id) {
  state.selectedEdge = id;
  state.selectedService = null;
  renderGraph();
  openTab('config');
  renderConfigPanel(id);
}
function selectService(id) {
  state.selectedService = id;
  $('#fault-service').value = id;
  renderGraph();
  // also show incident chain info + set tab to config with edges of service
  openTab('config');
  renderServicePanel(id);
}

// ---------------------------------------------------------------- tabs
function openTab(name) {
  state.tab = name;
  $$('.tabs button').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  if (name === 'config') {
    if (state.selectedEdge) renderConfigPanel(state.selectedEdge);
    else if (state.selectedService) renderServicePanel(state.selectedService);
    else renderConfigEmpty();
  } else if (name === 'inversions') renderInversions();
  else if (name === 'amplification') renderAmplification();
  else if (name === 'simulation') renderSimulationPanel();
  else if (name === 'proposals') renderProposals();
  else if (name === 'verify') renderVerify();
}

function panel(html) { $('#panel').innerHTML = html; }

function renderConfigEmpty() {
  panel(`<div class="empty">点左边拓扑里的<b>边</b>，查看三个来源如何合并出最终的超时与重试。<br>点<b>节点</b>可把它设为故障点。</div>`);
}

async function renderConfigPanel(edgeId) {
  panel(`<div class="empty"><span class="spinner"></span> 读取配置…</div>`);
  const d = await api('/api/edge/' + encodeURIComponent(edgeId));
  const r = d.resolved, em = d.metrics;
  const srcCards = d.sources.map(s => {
    const winner = Object.values(d.origins).some(o => o.source === s.source);
    return `<div class="src ${winner ? 'winner' : ''}">
      <div class="head">
        <span class="name">${esc(s.label || s.source_label)}</span>
        <span class="pill ${s.layer === 'infra' ? 'accent' : ''}">${s.layer === 'infra' ? '基础设施层' : '应用层'} · 优先级 ${s.priority}</span>
      </div>
      <div class="fields">${esc(s.describe)}</div>
    </div>`;
  }).join('');

  const originRows = Object.entries(d.origins).map(([f, o]) => {
    const label = FIELD_LABELS[f] || f;
    let val;
    if (o.value === null || o.value === undefined) val = '未设置';
    else if (typeof o.value === 'boolean') val = o.value ? '开' : '关';
    else if (f === 'retry_budget' && o.value === null) val = '无';
    else val = o.value;
    return `<div class="origin-row"><span class="f">${label}</span><span class="v">${esc(String(val))}</span><span class="o">← ${esc(o.label)}</span></div>`;
  }).join('');

  panel(`
    <div class="section">
      <div class="chain"><span class="n">${esc(d.edge.caller)}</span><span class="arrow">→</span><span class="n">${esc(d.edge.callee)}</span>
        ${em.inverted ? '<span class="pill bad">超时倒挂</span>' : ''}
        ${r.attempts > 1 ? `<span class="pill warn">最多 ${r.attempts} 次尝试</span>` : `<span class="pill good">不重试</span>`}
        ${r.retry_budget ? `<span class="pill accent">预算 ${pct(r.retry_budget)}</span>` : ''}
      </div>
      <div class="kv">
        <div class="k">最终超时（单次）</div><div class="v">${fmtMs(r.timeout_ms)}</div>
        <div class="k">最终尝试次数</div><div class="v">${r.attempts}（应用层 ${r.app_retries} 重试 × 网格层 ${r.mesh_retries} 重试）</div>
        <div class="k">最坏总耗时</div><div class="v">${fmtMs(em.downstream_ms)}</div>
        <div class="k">上游愿意等</div><div class="v">${fmtMs(em.upstream_budget_ms)}</div>
        ${em.inverted ? `<div class="k bad">上游放弃后下游空转</div><div class="v bad">${fmtMs(em.orphan_ms)}</div>` : ''}
        <div class="k">超时传递</div><div class="v">${r.propagate_deadline ? '开（下游能及时中止）' : '关（下游看不见上游预算）'}</div>
      </div>
    </div>
    <div class="section">
      <h3>合并规则</h3>
      <div class="card" style="font-size:11px;line-height:1.6">
        <div><b>层内覆盖：</b>应用层里配置中心 &gt; 代码注解；同源内精确匹配 &gt; 通配 &gt; 全局。</div>
        <div><b>跨层叠加：</b>超时取 <b>min</b>（谁先放弃谁生效）；重试 <b>相乘</b>（应用层与网格层串联）。</div>
        <div class="muted" style="margin-top:4px">超时：${esc(d.explain.timeout_ms)}</div>
        <div class="muted">重试：${esc(d.explain.retries)}</div>
      </div>
    </div>
    <div class="section">
      <h3>三个来源的原始配置</h3>
      <div class="src-list">${srcCards || '<div class="muted">这条边没有任何显式配置，全部走框架默认。</div>'}</div>
    </div>
    <div class="section">
      <h3>逐字段来源</h3>
      <div class="card">${originRows}</div>
    </div>
  `);
}

const FIELD_LABELS = {
  timeout_ms: '超时', retries: '重试', backoff_base_ms: '退避基数', backoff_mult: '退避倍数',
  backoff_max_ms: '退避上限', jitter: '抖动', retry_budget: '重试预算', propagate_deadline: '超时传递',
};

async function renderServicePanel(id) {
  const n = state.topo.nodes.find(x => x.id === id);
  if (!n) { renderConfigEmpty(); return; }
  const outs = state.topo.edges.filter(e => e.source === id);
  const ins = state.topo.edges.filter(e => e.target === id);
  const edgeRow = (e) => `<tr class="clickable" data-edge="${esc(e.id)}">
      <td>${esc(e.source === id ? '→ ' + e.target : e.source + ' →')}</td>
      <td class="num">${fmtMs(e.timeout_ms)}</td>
      <td class="num">${e.attempts}</td>
      <td class="num">${e.inverted ? '<span class="bad">⚠</span>' : ''}</td>
    </tr>`;
  panel(`
    <div class="section">
      <div class="chain"><span class="n ${n.incident ? 'incident' : ''}">${esc(n.name)}</span>
        <span class="pill">${esc(n.domain)} · tier ${n.tier}</span>
        ${n.root ? '<span class="pill good">入口</span>' : ''}
        ${n.incident ? '<span class="pill incident">事故链</span>' : ''}
      </div>
      <div class="kv">
        <div class="k">worker / 处理耗时</div><div class="v">${n.workers} 并发 · ${fmtMs(n.service_ms)}</div>
        <div class="k">理论上限</div><div class="v">${fmtNum(n.workers * 1000 / Math.max(1, n.service_ms), 0)} rps</div>
        <div class="k">可用作故障点</div><div class="v">已选中（上方“推演”按钮）</div>
      </div>
    </div>
    <div class="section"><h3>出边（它调用谁）</h3>
      <table><thead><tr><th>目标</th><th class="num">超时</th><th class="num">尝试</th><th></th></tr></thead>
      <tbody>${outs.map(edgeRow).join('') || '<tr><td colspan=4 class="muted">无</td></tr>'}</tbody></table>
    </div>
    <div class="section"><h3>入边（谁调用它）</h3>
      <table><thead><tr><th>来源</th><th class="num">超时</th><th class="num">尝试</th><th></th></tr></thead>
      <tbody>${ins.map(edgeRow).join('') || '<tr><td colspan=4 class="muted">无</td></tr>'}</tbody></table>
    </div>
  `);
  $$('#panel tr[data-edge]').forEach(tr => tr.addEventListener('click', () => selectEdge(tr.dataset.edge)));
}

// ---------------------------------------------------------------- inversions
async function renderInversions() {
  panel(`<div class="empty"><span class="spinner"></span> 分析中…</div>`);
  const d = await api('/api/inversions?limit=300');
  const rows = d.rows.map(r => `<tr class="clickable" data-edge="${esc(r.edge_id)}">
    <td class="mono">${esc(r.caller)}<br><span class="muted">→ ${esc(r.callee)}</span></td>
    <td class="num">${fmtMs(r.downstream_ms)}</td>
    <td class="num">${fmtMs(r.upstream_budget_ms)}</td>
    <td class="num bad">${fmtMs(r.orphan_ms)}</td>
    <td><span class="pill ${r.severity === 'high' ? 'bad' : 'warn'}">${r.severity}</span></td>
  </tr>`).join('');
  panel(`
    <div class="section">
      <h3>超时倒挂：${d.count} / ${d.total_edges} 条边</h3>
      <div class="card muted" style="font-size:11px">
        判据：下游把重试全部做完的最坏耗时 &gt; 上游愿意等的时间。
        “空转”= 上游已超时放弃、下游仍在重试的时间。
      </div>
      <table><thead><tr><th>调用边</th><th class="num">下游最坏</th><th class="num">上游预算</th><th class="num">空转</th><th></th></tr></thead>
      <tbody>${rows || '<tr><td colspan=5 class="muted">没有倒挂</td></tr>'}</tbody></table>
    </div>`);
  $$('#panel tr[data-edge]').forEach(tr => tr.addEventListener('click', () => selectEdge(tr.dataset.edge)));
}

// ---------------------------------------------------------------- amplification
async function renderAmplification() {
  panel(`<div class="empty"><span class="spinner"></span> 计算放大…</div>`);
  const d = await api('/api/amplification?limit=50');
  const row = (c) => {
    const layers = c.layers.map(l =>
      `<span class="n" title="${esc(l.edge_id)}">${esc(l.edge_id.split('->')[1])} ×${l.attempts}${l.inverted ? ' ⚠' : ''}${l.propagate_deadline ? ' ↯' : ''}</span>`
    ).join('<span class="arrow">×</span>');
    return `<div class="card">
      <div class="title">
        <div class="chain">${layers}</div>
        <span class="pill ${c.leaf_amplification >= 16 ? 'bad' : c.leaf_amplification > 4 ? 'warn' : 'good'}">×${c.leaf_amplification}</span>
      </div>
      <div class="kv">
        <div class="k">路径</div><div class="v mono">${esc(c.nodes.join(' → '))}</div>
        <div class="k">最坏总耗时</div><div class="v">${fmtMs(c.total_worst_ms)} ${c.budget_ms ? ' / 预算 ' + fmtMs(c.budget_ms) : ''} ${c.blown_budget ? '<span class="bad">超预算</span>' : ''}</div>
        <div class="k">危险叠加层数</div><div class="v">${c.danger_layers} ${c.inverted_edges.length ? '<span class="bad">· ' + c.inverted_edges.length + ' 处倒挂</span>' : ''}</div>
        ${c.is_incident ? '<div class="k"></div><div class="v"><span class="pill incident">上周事故链</span></div>' : ''}
      </div>
    </div>`;
  };
  panel(`
    <div class="section">
      <h3>最坏情况重试放大（按叶子请求数 / 入口请求数）</h3>
      <div class="card muted" style="font-size:11px">
        串行链路上，放大 = Π(每跳尝试次数)。标 ⚠ 的是倒挂层，↯ 表示开了超时传递。
      </div>
      ${d.rows.slice(0, 30).map(row).join('')}
    </div>`);
}

// ---------------------------------------------------------------- simulation
async function runSimulation() {
  const body = {
    service: $('#fault-service').value,
    slow_factor: parseFloat($('#fault-slow').value) || 1,
    error_rate: parseFloat($('#fault-err').value) || 0,
    start_ms: 5000,
    duration_ms: 60000,
    entry_rps: parseFloat($('#entry-rps').value) || 4,
  };
  setStatus('推演中（时间步进）…');
  $('#btn-simulate').disabled = true;
  try {
    const d = await api('/api/simulate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    loadSimulation(d);
    openTab('simulation');
  } finally {
    $('#btn-simulate').disabled = false;
    setStatus('');
  }
}

async function reproduceIncident() {
  setStatus('复现上周故障…');
  const rps = parseFloat($('#entry-rps').value) || 4;
  const d = await api('/api/incident?entry_rps=' + rps + '&duration_ms=60000');
  loadSimulation(d.simulation);
  state.incidentResult = d;
  $('#fault-service').value = 'inventory-svc-01';
  openTab('simulation');
  // add marker for fault start
  setStatus('已复现：入口错误率 ' + pct(d.verdict.root_error_rate));
}

function loadSimulation(d) {
  state.sim = d;
  state.frames = d.frames || [];
  state.frameIdx = 0;
  buildTimeline(d);
  updatePlaybackEnabled(true);
  applyFrame();
  renderGraph();
}

function buildTimeline(d) {
  const range = $('#tl-range');
  range.max = Math.max(0, state.frames.length - 1);
  range.value = 0;
  // ticks
  const tl = $('#timeline');
  $$('.tick, .marker, .marker-label', tl).forEach(el => el.remove());
  const dur = d.duration_ms;
  const nTicks = 6;
  for (let i = 0; i <= nTicks; i++) {
    const t = dur * i / nTicks;
    const tick = document.createElement('div');
    tick.className = 'tick';
    tick.style.left = (i / nTicks * 100) + '%';
    tick.textContent = (t / 1000).toFixed(0) + 's';
    tl.appendChild(tick);
  }
  // fault marker
  const faults = (d.scenario && d.scenario.faults) || [];
  faults.forEach(f => {
    if (!f.slow_factor && !f.error_rate) return;
    const frac = Math.max(0, Math.min(1, (f.start_ms || 0) / dur));
    const m = document.createElement('div');
    m.className = 'marker';
    m.style.left = (frac * 100) + '%';
    tl.appendChild(m);
    const l = document.createElement('div');
    l.className = 'marker-label';
    l.style.left = (frac * 100) + '%';
    l.textContent = '故障注入';
    tl.appendChild(l);
  });
}

function updatePlaybackEnabled(on) {
  ['#btn-play', '#btn-step-back', '#btn-step-fwd'].forEach(s => { $(s).disabled = !on; });
  $('#tl-range').disabled = !on;
}

function applyFrame() {
  const n = state.frames.length;
  if (!n) return;
  const idx = Math.max(0, Math.min(n - 1, state.frameIdx));
  state.frameIdx = idx;
  const f = state.frames[idx];
  const frac = n <= 1 ? 0 : idx / (n - 1);
  $('#tl-fill').style.width = (frac * 100) + '%';
  $('#tl-head').style.left = (frac * 100) + '%';
  $('#tl-range').value = idx;
  $('#frame-label').innerHTML = `t = <b>${(f.t / 1000).toFixed(2)}s</b> · 帧 ${idx + 1}/${n}`;
  renderHUD(f);
  renderGraph();
  // live saturation summary in header
  if (state.sim) {
    const sat = Object.entries(f.services).filter(([k, v]) => v.queue >= (v.workers || 1) * 4);
    setStatus(sat.length ? `过载服务：${sat.map(s => s[0]).join(', ')}` : '');
  }
}

function renderHUD(f) {
  const hud = $('#hud');
  const sim = state.sim;
  if (!hud || !sim) { if (hud) hud.classList.remove('show'); return; }
  hud.classList.add('show');

  // 事故链 / 主要边：取每条边累计放大与窗口流量
  const edges = Object.entries(f.edges || {});
  // 只显示本帧有活动的边（窗口流量 > 0）或事故链边
  const inc = new Set(state.incidentServices);
  const active = edges.filter(([eid, e]) => e.window > 0 || e.timeout_window > 0);
  active.sort((a, b) => (b[1].window + b[1].timeout_window * 2) - (a[1].window + a[1].timeout_window * 2));
  const top = active.slice(0, 5);
  const maxWin = Math.max(1, ...top.map(([, e]) => e.window + e.timeout_window * 2));

  // root 累计
  const cum = sim.edge_stats || {};
  const rows = top.map(([eid, e]) => {
    const shortName = eid.replace('->', ' → ');
    const total = (cum[eid] && cum[eid].attempts) || e.attempts;
    const amp = sim.root_requests ? (total / sim.root_requests) : 0;
    const width = Math.min(100, ((e.window + e.timeout_window * 2) / maxWin) * 100);
    const tos = e.timeout_window ? `<span class="bad">超时 ${e.timeout_window}</span>` : '';
    return `<div class="hud-edge">
      <div class="top"><span class="name">${esc(shortName)}</span>
        <span class="muted">窗口 ${e.window} ${tos}</span></div>
      <div class="top"><span class="muted" style="font-size:10px">累计放大 ×${fmtNum(amp, 1)}</span></div>
      <div class="bar"><div style="width:${width}%"></div></div>
    </div>`;
  }).join('');

  hud.innerHTML = `<div class="hud-title">t = ${(f.t / 1000).toFixed(1)}s · 流量放大实况</div>
    <div class="hud-row"><span class="lbl">本帧入口请求</span><span class="val">${f.root_arrivals || 0}</span></div>
    ${rows || '<div class="muted" style="font-size:10.5px">本帧无活动</div>'}`;
}

function togglePlay() { state.playing ? pause() : play(); }
function play() {
  if (!state.frames.length) return;
  if (state.frameIdx >= state.frames.length - 1) state.frameIdx = 0;
  state.playing = true;
  $('#btn-play').textContent = '❚❚';
  const speed = parseFloat($('#play-speed').value) || 1;
  const interval = Math.max(16, 250 / speed);
  clearInterval(state.playTimer);
  state.playTimer = setInterval(() => {
    if (state.frameIdx >= state.frames.length - 1) { pause(); return; }
    state.frameIdx++;
    applyFrame();
  }, interval);
  renderGraph();
}
function pause() {
  state.playing = false;
  clearInterval(state.playTimer);
  $('#btn-play').textContent = '▶';
  $('#graph').classList.remove('pulse');
}
function stepFrame(dir) {
  state.frameIdx = Math.max(0, Math.min(state.frames.length - 1, state.frameIdx + dir));
  applyFrame();
}

function renderSimulationPanel() {
  const d = state.sim;
  if (!d) {
    $('#hud') && $('#hud').classList.remove('show');
    panel(`<div class="empty">还没推演。选好故障点和参数，点上方<b>推演</b>；或点右上角<b>复现上周故障</b>。</div>`);
    return;
  }
  const s = d.saturation || {};
  const satRows = (s.saturated_services || []).map(x => `<tr>
      <td class="mono">${esc(x.service)}</td>
      <td class="num">${x.max_queue}</td>
      <td class="num">${pct(x.peak_util)}</td>
      <td class="num">${x.rejected}</td>
    </tr>`).join('') || '<tr><td colspan=4 class="muted">无服务过载</td></tr>';

  const edgeRows = Object.entries(d.edge_stats).map(([eid, st]) => {
    const amp = st.amplification || 0;
    const color = amp >= 8 ? 'var(--bad)' : amp > 3 ? 'var(--warn)' : 'var(--accent)';
    return `<tr>
      <td class="mono">${esc(eid)}</td>
      <td class="num">${st.attempts}</td>
      <td class="num">${st.timeouts || 0}</td>
      <td class="num">${st.orphaned || 0}</td>
      <td class="num">${st.rejected || 0}</td>
      <td class="num" style="color:${color};font-weight:600">×${fmtNum(amp, 2)}</td>
    </tr>`;
  }).join('');

  const faults = (d.scenario && d.scenario.faults) || [];
  const faultDesc = faults.map(f => `${f.service}（慢 ${f.slow_factor}×，错 ${pct(f.error_rate)}，${(f.start_ms/1000).toFixed(0)}s 起）`).join('；') || '无故障注入';

  panel(`
    <div class="section">
      <div class="verdict-banner ${(s.root_error_rate||0) > 0.3 ? 'bad' : 'good'}">
        <div class="big">入口请求 ${d.root_requests} · 错误率 ${pct(s.root_error_rate || 0)}</div>
        <div class="muted" style="font-size:11px">故障：${esc(faultDesc)}</div>
      </div>
    </div>
    <div class="section">
      <h3>被打到容量上限的服务（${s.n_saturated || 0}）</h3>
      <table><thead><tr><th>服务</th><th class="num">最大排队</th><th class="num">峰值占用</th><th class="num">被拒</th></tr></thead>
      <tbody>${satRows}</tbody></table>
    </div>
    <div class="section">
      <h3>各调用边流量放大（尝试数 / 入口请求数）</h3>
      <table><thead><tr><th>调用边</th><th class="num">尝试</th><th class="num">超时</th><th class="num">空转</th><th class="num">拒绝</th><th class="num">放大</th></tr></thead>
      <tbody>${edgeRows}</tbody></table>
    </div>
  `);
}

// ---------------------------------------------------------------- proposals
async function renderProposals() {
  panel(`<div class="empty"><span class="spinner"></span> 生成建议…</div>`);
  const d = await api('/api/proposals');
  const cards = d.proposals.map(p => {
    const b = p.before, a = p.after;
    const changes = p.changes.slice(0, 30).map(c =>
      `<div class="change"><span class="mono">${esc(c.edge_id)}</span> · ${FIELD_LABELS[c.field] || c.field}
        <span class="old">${esc(String(c.old))}</span> → <span class="new">${esc(String(c.new))}</span>
        ${c.note ? '<span class="muted">（' + esc(c.note) + '）</span>' : ''}</div>`
    ).join('');
    return `<div class="card">
      <div class="title"><b>${esc(p.title)}</b>
        <span class="pill accent">${p.n_changes} 处改动</span></div>
      <div class="muted" style="font-size:11px;margin:5px 0">${esc(p.rationale)}</div>
      <table><thead><tr><th></th><th class="num">事故链放大</th><th class="num">最大放大</th><th class="num">倒挂边</th></tr></thead>
      <tbody>
        <tr><td>改前</td><td class="num bad">×${b.incident_amplification ?? '–'}</td><td class="num">×${b.max_leaf_amplification ?? '–'}</td><td class="num">${b.n_inverted_edges ?? '–'}</td></tr>
        <tr><td>改后</td><td class="num ok">×${a.incident_amplification ?? '–'}</td><td class="num">×${a.max_leaf_amplification ?? '–'}</td><td class="num">${a.n_inverted_edges ?? '–'}</td></tr>
      </tbody></table>
      <div style="margin-top:8px">
        <button class="btn primary" data-eval="${esc(p.key)}">改完再推演一遍</button>
      </div>
      <details style="margin-top:8px"><summary>查看 ${p.n_changes} 处具体改动</summary>${changes || '<div class="muted">无</div>'}</details>
      <div id="eval-${esc(p.key)}"></div>
    </div>`;
  }).join('');
  panel(`
    <div class="section">
      <h3>修改建议（点击可在推演中验证效果）</h3>
      <div class="card muted" style="font-size:11px">
        每条建议都是对三来源配置的真实改写（以最高优先级“建议方案”规则叠加），
        下方“改前 / 改后”是静态重算的结果，点按钮会跑完整时间步进推演。
      </div>
      ${cards}
    </div>`);
  $$('#panel button[data-eval]').forEach(btn => {
    btn.addEventListener('click', () => evaluateProposal(btn.dataset.eval, btn));
  });
}

async function evaluateProposal(key, btn) {
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> 推演中…';
  try {
    const d = await api('/api/proposals/' + encodeURIComponent(key) + '/evaluate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}),
    });
    const b = d.before, a = d.after;
    const chainRows = Object.keys(b.chain_amplification).map(eid => {
      const bv = b.chain_amplification[eid], av = a.chain_amplification[eid];
      const arrow = av < bv - 0.05 ? '<span class="ok">↓</span>' : (Math.abs(av - bv) < 0.05 ? '=' : '<span class="bad">↑</span>');
      return `<tr><td class="mono">${esc(eid)}</td><td class="num">×${fmtNum(bv, 2)}</td><td class="num">${arrow}</td><td class="num ok">×${fmtNum(av, 2)}</td></tr>`;
    }).join('');
    const html = `
      <div class="verdict-banner ${a.root_error_rate < b.root_error_rate ? 'good' : 'bad'}" style="margin-top:8px">
        <div class="big">入口错误率 ${pct(b.root_error_rate)} → ${pct(a.root_error_rate)}</div>
        <div class="muted" style="font-size:11px">过载服务 ${b.n_saturated} → ${a.n_saturated} 个</div>
      </div>
      <table><thead><tr><th>事故链边</th><th class="num">改前放大</th><th></th><th class="num">改后放大</th></tr></thead>
      <tbody>${chainRows}</tbody></table>`;
    $('#eval-' + key).innerHTML = html;
    btn.textContent = '已复演 ✓';
  } catch (e) {
    btn.textContent = '重试';
    btn.disabled = false;
  }
}

// ---------------------------------------------------------------- verify
async function renderVerify() {
  panel(`<div class="empty"><span class="spinner"></span> 运行两台模拟器核对…</div>`);
  let d;
  try {
    d = await api('/api/verify/incident?n_requests=40');
  } catch (e) {
    panel(`<div class="empty bad">核对失败：${esc(e.message)}</div>`);
    return;
  }
  const c = d.converged;
  const rows = c.rows.map(r => `<tr>
    <td class="mono">${esc(r.edge_id)}</td>
    <td class="num">×${fmtNum(r.analytical, 3)}</td>
    <td class="num">×${fmtNum(r.stepped, 3)}</td>
    <td class="num">×${fmtNum(r.des, 3)}</td>
    <td class="num ${r.ok ? 'ok' : 'bad'}">${r.ok ? '✓' : '✗'}</td>
  </tr>`).join('');

  panel(`
    <div class="section">
      <div class="verdict-banner ${c.ok ? 'good' : 'bad'}">
        <div class="big">${c.ok ? '✓ 三方一致' : '✗ 存在偏差'}</div>
        <div style="font-size:11px">解析公式 · 时间步进推演 · 逐请求离散事件模拟，最大相对偏差 ${c.max_rel_err < 1e-9 ? '0' : c.max_rel_err.toExponential(2)}</div>
      </div>
      <div class="card muted" style="font-size:11px">
        核对工况：事故链单独抽出、故障从 0 时刻生效、固定 ${c.n_root_requests} 个入口请求、
        发完排空到静止、确定性处理时长。此工况下三者的放大倍数应<b>精确相等</b>。
      </div>
      <table><thead><tr><th>调用边</th><th class="num">解析上限</th><th class="num">步进推演</th><th class="num">离散事件</th><th></th></tr></thead>
      <tbody>${rows}</tbody></table>
      <div class="notice">解析上限 = 沿链路 Π(每跳尝试次数)。时间步进引擎按 ${'{'}dt${'}'} 量化推进并用于动画；
        离散事件引擎逐请求精确排序事件。两者实现独立，仅共享配置策略函数。</div>
    </div>
  `);
}

// ---------------------------------------------------------------- boot
window.addEventListener('DOMContentLoaded', init);
window.addEventListener('resize', () => { if (state.topo) renderGraph(); });

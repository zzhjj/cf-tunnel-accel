/* ============================================================
   ARC console — 前端运行时
   纯原生：无框架、无构建、无外部请求。所有数据来自同源 /api。
   ============================================================ */
(() => {
  'use strict';

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const finePointer = window.matchMedia('(pointer: fine)').matches;

  const readCookie = (name) => {
    const m = document.cookie.match(new RegExp(`(?:^|; )${name}=([^;]*)`));
    return m ? decodeURIComponent(m[1]) : '';
  };

  const S = {
    csrf: readCookie('arc_csrf'),
    state: null,
    services: [],
    journal: [],
    filter: { services: '', routes: '', scope: 'all' },
    target: null,
    job: null,
    paletteIdx: 0,
    paletteItems: [],
    diagTimer: null,
  };

  /* ── HTTP ─────────────────────────────────────────────── */
  async function api(path, { method = 'GET', body } = {}) {
    const headers = { 'Accept': 'application/json' };
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    if (S.csrf && method !== 'GET') headers['X-CSRF'] = S.csrf;
    const res = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: 'same-origin',
      cache: 'no-store',
    });
    let data = null;
    try { data = await res.json(); } catch (_) { /* 204 或空体 */ }
    if (!res.ok) {
      const err = new Error((data && data.error) || `HTTP ${res.status}`);
      err.status = res.status;
      err.data = data;
      throw err;
    }
    return data || {};
  }

  /* ── 小工具 ───────────────────────────────────────────── */
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));

  const el = (tag, cls, html) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (html !== undefined) node.innerHTML = html;
    return node;
  };

  function toast(title, msg = '', kind = '') {
    const box = $('#toasts');
    const node = el('div', `toast ${kind}`, `<b>${esc(title)}</b>${msg ? esc(msg) : ''}`);
    box.append(node);
    setTimeout(() => {
      node.classList.add('out');
      setTimeout(() => node.remove(), 400);
    }, kind === 'bad' ? 7000 : 4200);
  }

  /* ── 入场动效 ─────────────────────────────────────────── */
  function initReveal() {
    const io = new IntersectionObserver((entries) => {
      entries.forEach((e) => {
        if (e.isIntersecting) {
          e.target.classList.add('in');
          io.unobserve(e.target);
        }
      });
    }, { rootMargin: '0px 0px -8% 0px', threshold: 0.05 });
    $$('.reveal').forEach((n) => io.observe(n));
  }

  function initRail() {
    const sections = ['services', 'routes', 'diagnostics', 'journal'];
    const links = new Map($$('.rail a').map((a) => [a.dataset.rail, a]));
    const bar = $('#rail-bar');
    const onScroll = () => {
      const doc = document.documentElement;
      const pct = Math.min(1, Math.max(0, doc.scrollTop / (doc.scrollHeight - doc.clientHeight || 1)));
      bar.style.height = `${pct * 100}%`;
      let current = null;
      sections.forEach((id) => {
        const box = document.getElementById(id);
        if (box && box.getBoundingClientRect().top < window.innerHeight * 0.42) current = id;
      });
      links.forEach((a, id) => a.classList.toggle('active', id === current));
    };
    window.addEventListener('scroll', onScroll, { passive: true });
    onScroll();
  }

  function initCursor() {
    if (!finePointer || reduceMotion) return;
    const cur = $('#cursor');
    const dot = $('.cursor-dot', cur);
    const ring = $('.cursor-ring', cur);
    let mx = window.innerWidth / 2, my = window.innerHeight / 2;
    let rx = mx, ry = my;
    let shown = false;
    window.addEventListener('pointermove', (e) => {
      if (!shown) { shown = true; cur.classList.add('on'); }
      mx = e.clientX; my = e.clientY;
      dot.style.transform = `translate(${mx}px, ${my}px) translate(-50%, -50%)`;
      const hot = e.target.closest('button, a, input, .route, .listener, .chip');
      cur.classList.toggle('is-hot', Boolean(hot));
    });
    const loop = () => {
      rx += (mx - rx) * 0.16;
      ry += (my - ry) * 0.16;
      ring.style.transform = `translate(${rx}px, ${ry}px) translate(-50%, -50%)`;
      requestAnimationFrame(loop);
    };
    loop();
  }

  /* ── 跑马灯 / 顶栏 ────────────────────────────────────── */
  function renderTicker() {
    const d = S.state;
    if (!d) return;
    const items = [
      `<span>TUNNEL <b>${esc(d.service.name.split('.')[0])}</b> ${d.service.active ? '<b>ACTIVE</b>' : 'DOWN'}</span>`,
      `<span>ENTRY <b>${esc(d.preferred.entry)}</b> → ${esc(d.preferred.target || '—')}</span>`,
      `<span>ROUTES <b>${d.counts.routes}</b></span>`,
      `<span>ZONES <b>${d.counts.zones}</b></span>`,
      `<span>SAAS <b>${d.counts.saas}</b></span>`,
      `<span>FALLBACK <b>${esc(d.fallback)}</b></span>`,
    ];
    const line = items.join('');
    $('#ticker-track').innerHTML = line + line;

    const mk = (rows) => rows.concat(rows).map((r) => `<span>${esc(r)}</span>`).join('');
    $('#marquee-a').innerHTML = mk([
      `${d.counts.routes} ROUTES RIDING ONE TUNNEL`,
      `SIGNAL OVER TUNNEL`,
      `ENTRY ${d.preferred.entry} → ${d.preferred.target || ''}`,
      `SAAS ORIGIN ${d.fallback}`,
    ]);
    $('#marquee-b').innerHTML = mk([
      `LOCAL SERVICES ${S.services.length}`,
      `MAPPED ${S.services.filter((s) => s.mapped.length).length}`,
      `MODULE v${d.version}`,
      `AUDIT ON · CSRF ON · CSP ON`,
    ]);
  }

  function renderStats() {
    const d = S.state;
    if (!d) return;
    const set = (k, v) => { const n = $(`[data-stat="${k}"]`); if (n) n.textContent = v; };
    const countTo = (node, value, suffix = '') => {
      if (!node) return;
      if (reduceMotion) { node.textContent = `${value}${suffix}`; return; }
      const from = 0;
      const t0 = performance.now();
      const dur = 900;
      const step = (t) => {
        const p = Math.min(1, (t - t0) / dur);
        const eased = 1 - Math.pow(1 - p, 3);
        node.textContent = `${Math.round(from + (value - from) * eased)}${suffix}`;
        if (p < 1) requestAnimationFrame(step);
      };
      requestAnimationFrame(step);
    };
    countTo($('[data-stat="routes"]'), d.counts.routes);
    countTo($('[data-stat="zones"]'), d.counts.zones);
    countTo($('[data-stat="saas"]'), d.counts.saas);
    const tn = $('[data-stat="tunnel"]');
    if (tn) tn.textContent = d.service.active ? 'ACTIVE' : 'DOWN';
    $('#hero-version').textContent = `v${d.version}`;
    $('#foot-config').textContent = d.config_path;
    $('#foot-service').textContent = `${d.service.name} · pid ${d.service.mainpid || '—'}`;
  }

  /* ── LISTENERS ────────────────────────────────────────── */
  function renderListeners() {
    const box = $('#listener-list');
    const q = S.filter.services.toLowerCase();
    const scope = S.filter.scope;
    const rows = S.services.filter((s) => {
      if (scope === 'mapped' && !s.mapped.length) return false;
      if (scope === 'free' && s.mapped.length) return false;
      if (!q) return true;
      return [String(s.port), s.process, s.friendly, s.cmd, s.mapped.join(' ')]
        .join(' ').toLowerCase().includes(q);
    });
    if (!rows.length) {
      box.innerHTML = '<div class="listener"><span class="listener-port">—</span>'
        + '<div class="listener-name"><b>没有匹配的监听端口</b><span>换个关键词，或点右上角刷新</span></div>'
        + '<div></div><div></div></div>';
      return;
    }
    box.innerHTML = rows.map((s) => {
      const probe = s.probe || {};
      const mapped = s.mapped.length
        ? s.mapped.map((h) => `<button class="chip" data-goto="${esc(h)}">${esc(h)}</button>`).join('')
        : '<span class="chip ghost">未映射</span>';
      return `<div class="listener">
        <div class="listener-port">${s.port}<em>${s.addr === '127.0.0.1' || s.addr === '::1' ? 'LOCAL' : 'ALL'}</em></div>
        <div class="listener-name">
          <b>${esc(s.friendly)}</b>
          <span title="${esc(s.cmd)}">${esc(s.cmd || s.process || '—')}</span>
        </div>
        <div class="listener-hosts">${mapped}</div>
        <div class="listener-act">
          <span class="probe ${probe.ok ? 'ok' : 'bad'}" title="${esc(probe.detail || '')}">${probe.ok ? 'UP' : 'DOWN'}</span>
          <button class="ghost-btn" data-bind="${s.port}">映射域名</button>
        </div>
      </div>`;
    }).join('');

    $$('[data-bind]', box).forEach((b) => b.addEventListener('click', () => {
      openAddModal(`http://127.0.0.1:${b.dataset.bind}`);
    }));
    $$('[data-goto]', box).forEach((b) => b.addEventListener('click', () => focusRoute(b.dataset.goto)));
  }

  /* ── ROUTES ───────────────────────────────────────────── */
  const saasBadge = (saas) => {
    if (!saas) return '<span class="badge">no saas</span>';
    const cls = saas.status === 'active' && saas.ssl === 'active' ? 'sig'
      : (saas.ssl === 'pending_validation' || saas.ssl === 'pending_issuance' || saas.ssl === 'expired') ? 'warn'
      : saas.status && saas.status !== 'active' ? 'bad' : '';
    return `<span class="badge ${cls}">${esc(saas.status || '?')}/${esc(saas.ssl || '?')}</span>`;
  };

  function renderRoutes() {
    const box = $('#route-grid');
    const q = S.filter.routes.toLowerCase();
    const rows = S.state.routes.filter((r) => {
      if (!q) return true;
      return [r.hostname, r.service, r.zone || '', r.external ? 'external' : '']
        .join(' ').toLowerCase().includes(q);
    });
    if (!rows.length) {
      box.innerHTML = `<div class="route"><div class="route-host">没有匹配的域名</div>
        <div class="route-meta"><div><span>提示</span><b>清空过滤或新增一个域名</b></div></div></div>`;
      return;
    }
    box.innerHTML = rows.map((r) => {
      const [scheme, hostport] = (r.service || ':::').split('://');
      const port = (hostport || '').split(':').pop();
      return `<article class="route ${S.target === r.hostname ? 'is-target' : ''}" data-host="${esc(r.hostname)}">
        <div class="route-top">
          <h3 class="route-host">${esc(r.hostname)}</h3>
          ${r.external ? '<span class="badge warn">external</span>' : `<span class="badge">${esc(r.zone || '—')}</span>`}
        </div>
        <div class="route-meta">
          <div><span>→ backend</span><b title="${esc(r.service)}">${esc(scheme || '')}://${esc(hostport || '')}</b></div>
          <div><span>port</span><b>${esc(port || '—')}</b></div>
        </div>
        <div class="route-badges">${saasBadge(r.saas)}</div>
        <div class="route-actions">
          <button data-act="edit" data-host="${esc(r.hostname)}">改后端</button>
          <button data-act="switch" data-host="${esc(r.hostname)}">切优选</button>
          <button data-act="health" data-host="${esc(r.hostname)}">体检</button>
          <button data-act="open" data-host="${esc(r.hostname)}">详情</button>
          <button class="del" data-act="del" data-host="${esc(r.hostname)}">删除</button>
        </div>
      </article>`;
    }).join('');

    $$('[data-act]', box).forEach((b) => b.addEventListener('click', (ev) => {
      ev.stopPropagation();
      const host = b.dataset.host;
      const act = b.dataset.act;
      if (act === 'edit') openEditModal(host);
      if (act === 'switch') confirmSwitch(host);
      if (act === 'health') runJob({ op: 'health', hostname: host, https: true });
      if (act === 'del') confirmDelete(host);
      if (act === 'open') openDetail(host);
    }));
    $$('.route', box).forEach((card) => card.addEventListener('click', () => focusRoute(card.dataset.host, true)));
    setCount('count-routes', `${rows.length}/${S.state.routes.length}`);
  }

  function focusRoute(host, open = false) {
    S.target = host;
    $$('.route').forEach((c) => c.classList.toggle('is-target', c.dataset.host === host));
    const card = $(`.route[data-host="${CSS.escape(host)}"]`);
    if (card) {
      document.getElementById('routes').scrollIntoView({ behavior: reduceMotion ? 'auto' : 'smooth', block: 'start' });
      card.animate?.(
        [{ boxShadow: 'inset 0 0 0 1px rgba(200,255,60,.9)' }, { boxShadow: 'inset 0 0 0 1px rgba(200,255,60,0)' }],
        { duration: 1400, easing: 'cubic-bezier(.16,1,.3,1)' },
      );
    }
    if (open) openDetail(host);
  }

  /* ── JOURNAL ──────────────────────────────────────────── */
  function renderJournal() {
    const box = $('#journal-list');
    if (!S.journal.length) {
      box.innerHTML = '<div class="jrow"><span>—</span><span></span><span>还没有动作记录</span></div>';
      return;
    }
    box.innerHTML = S.journal.map((r) => {
      const kind = r.event.includes('error') || r.event.includes('fail') ? 'fail'
        : r.event.includes('ok') || r.event.includes('done') ? 'ok' : '';
      const detail = [r.op, r.hostname, r.actor || r.ip, r.error].filter(Boolean).join(' · ');
      const rel = r.ts ? r.ts.slice(11) : '';
      return `<div class="jrow">
        <span>${esc(rel)}</span>
        <span class="ev ${kind}">${esc(r.event || '')}</span>
        <span title="${esc(detail)}">${esc(detail || '—')}</span>
      </div>`;
    }).join('');
  }

  /* ── 任务 ─────────────────────────────────────────────── */
  function jobBar(payload) {
    const bar = $('#jobbar');
    bar.hidden = false;
    bar.classList.remove('open');
    $('#jobbar-op').textContent = (payload.op || 'job').toUpperCase();
    $('#jobbar-status').textContent = 'queued';
    $('#jobbar-status').className = 'jobbar-status';
    $('#jobbar-elapsed').textContent = '0.0s';
    $('#jobbar-progress').style.width = '4%';
    $('#jobbar-log').innerHTML = '';
  }

  async function runJob(payload) {
    if (S.job) {
      toast('有任务在跑', '等它结束再提交下一个', 'warn');
      return;
    }
    jobBar(payload);
    S.job = { since: 0, id: null };
    try {
      const started = await api('/api/jobs', { method: 'POST', body: payload });
      const id = started.job.id;
      S.job.id = id;
      await pollJob(id);
    } catch (err) {
      $('#jobbar-status').textContent = 'error';
      $('#jobbar-status').className = 'jobbar-status err';
      toast('任务未能提交', err.message, 'bad');
      S.job = null;
    }
  }

  function appendJobLines(lines) {
    const box = $('#jobbar-log');
    lines.forEach((l) => {
      const li = el('li');
      li.innerHTML = `<span class="t">${l.t.toFixed(1)}s</span><span>${esc(l.m)}</span>`;
      box.append(li);
    });
    if (lines.length) box.scrollTop = box.scrollHeight;
  }

  async function pollJob(id) {
    let since = 0;
    const t0 = performance.now();
    for (;;) {
      let res;
      try {
        res = await api(`/api/jobs/${id}?since=${since}`);
      } catch (err) {
        $('#jobbar-status').textContent = 'lost';
        $('#jobbar-status').className = 'jobbar-status err';
        toast('任务状态读取失败', err.message, 'bad');
        S.job = null;
        return;
      }
      const job = res.job;
      if (job.lines && job.lines.length) {
        appendJobLines(job.lines);
        if (job.op === 'health') job.lines.forEach((l) => {
          const cls = /FAIL/.test(l.m) ? 'fail' : /WARN/.test(l.m) ? 'warn'
            : /PASS/.test(l.m) ? 'pass' : /^\s*======/.test(l.m) ? 'head' : 'info';
          const li = el('li', cls);
          li.innerHTML = `<span class="t">${l.t.toFixed(1)}s</span><span class="m">${esc(l.m)}</span>`;
          $('#diag-lines').append(li);
          $('#diag-lines').parentElement.scrollTop = $('#diag-lines').parentElement.scrollHeight;
        });
        since = job.next;
      }
      const elapsed = ((performance.now() - t0) / 1000).toFixed(1);
      $('#jobbar-elapsed').textContent = `${job.elapsed.toFixed(1)}s`;
      $('#jobbar-status').textContent = job.status;
      $('#jobbar-status').className = `jobbar-status ${job.status === 'error' ? 'err' : job.status === 'done' ? 'done' : ''}`;
      const pct = job.status === 'done' ? 100 : Math.min(94, 8 + (performance.now() - t0) / 220);
      $('#jobbar-progress').style.width = `${pct}%`;
      if (job.status === 'done' || job.status === 'error') {
        if (job.error) {
          $('#jobbar-status').className = 'jobbar-status err';
          toast('任务失败', job.error, 'bad');
          $('#jobbar').classList.add('open');
        } else {
          toast('完成', `${job.op} · ${job.elapsed.toFixed(1)}s`, '');
        }
        S.job = null;
        await Promise.all([loadState(), loadServices(false), loadJournal()]);
        renderTicker();
        if (job.op === 'health') {
          const ok = job.result && job.result.ok;
          const li = el('li', ok ? 'pass' : 'fail');
          li.innerHTML = `<span class="t">done</span><span class="m">health_check → ${ok ? 'PASS' : 'FAIL'}</span>`;
          $('#diag-lines').append(li);
        }
        void elapsed;
        return;
      }
      await new Promise((r) => setTimeout(r, 700));
    }
  }

  /* ── 体检面板 ─────────────────────────────────────────── */
  function diagLine(cls, msg) {
    const li = el('li', cls);
    const t = ((performance.now() - (S.diagT0 || performance.now())) / 1000).toFixed(1);
    li.innerHTML = `<span class="t">${t}s</span><span class="m">${esc(msg)}</span>`;
    return li;
  }

  function renderDiag(rows) {
    const box = $('#diag-lines');
    $('#diag-empty').hidden = true;
    box.innerHTML = '';
    S.diagT0 = performance.now();
    rows.forEach((r) => box.append(diagLine(r.cls, r.m)));
  }

  async function runHealth() {
    const https = $('#diag-https').checked;
    $('#diag-lines').innerHTML = '';
    $('#diag-empty').hidden = true;
    S.diagT0 = performance.now();
    $('#diag-lines').append(diagLine('head', `全链路体检 · https=${https ? 'on' : 'off'}`));

    // 先跑一个本地即时检查，给用户立刻的反馈
    const s = await api('/api/probe?service=' + encodeURIComponent('http://127.0.0.1:1') + '&hostname=x').catch(() => null);
    if (s) $('#diag-lines').append(diagLine('info', 'API 通道就绪，开始调用 cloudflared / Cloudflare'));

    const timer = setInterval(() => {
      $('#diag-lines').append(diagLine('info', '…仍在进行（隧道重启与证书等待可能较久）'));
    }, 6000);
    S.diagTimer = timer;
    await runJob({ op: 'health', https });
    clearInterval(timer);
  }

  /* ── 模态框 ───────────────────────────────────────────── */
  function openModal({ kicker, title, body, okLabel = '确认', danger = false, hideCancel = false, onOk }) {
    const modal = $('#modal');
    $('#modal-kicker').textContent = kicker || 'ACTION';
    $('#modal-title').textContent = title || '';
    $('#modal-body').innerHTML = '';
    if (typeof body === 'string') $('#modal-body').innerHTML = body;
    else if (body) $('#modal-body').append(body);
    const ok = $('#modal-ok');
    ok.innerHTML = `<span>${esc(okLabel)}</span>`;
    ok.classList.toggle('danger', Boolean(danger));
    ok.className = `btn ${danger ? 'btn-wire danger' : 'btn-solid'}`;
    ok.innerHTML = `<span>${esc(okLabel)}</span>`;
    modal.hidden = false;
    const close = () => { modal.hidden = true; ok.onclick = null; };
    $('#modal-cancel').hidden = Boolean(hideCancel);
    $('#modal-cancel').onclick = close;
    modal.dataset.danger = danger ? '1' : '0';
    ok.onclick = async () => {
      ok.disabled = true;
      try {
        const keep = await onOk?.();
        if (keep !== false) close();
      } finally {
        ok.disabled = false;
      }
    };
    modal.onclick = (e) => { if (e.target === modal) close(); };
    modal.dataset.close = '1';
    modal._close = close;
    setTimeout(() => $('#modal-body input')?.focus(), 60);
    return { close };
  }

  function fieldHtml({ label, id, value = '', placeholder = '', hint = '' }) {
    return `<div class="mfield">
      <label for="${id}">${esc(label)}</label>
      <input id="${id}" value="${esc(value)}" placeholder="${esc(placeholder)}" autocomplete="off" spellcheck="false">
      ${hint ? `<span class="hint">${hint}</span>` : ''}
      <span class="err" id="${id}-err"></span>
    </div>`;
  }

  function openAddModal(presetService = '') {
    const zones = (S.state?.zones || []).filter((z) => z.status === 'active').map((z) => z.name);
    const quick = S.services.slice(0, 8)
      .map((s) => `<button type="button" data-svc="http://127.0.0.1:${s.port}">${s.port} · ${esc(s.friendly)}</button>`)
      .join('');
    const body = `${fieldHtml({ label: '域名', id: 'f-host', placeholder: 'app.261119.xyz', hint: zones.length ? `可用 zone：${esc(zones.join(' · '))}` : '' })}
      ${fieldHtml({ label: '后端', id: 'f-svc', value: presetService, placeholder: 'http://127.0.0.1:8080' })}
      <div class="quick-list">${quick}</div>
      <p>会依次完成：写隧道 ingress → 重启 → 建 SaaS 自定义主机名 → 写验证记录 → CNAME 到优选入口 → HTTPS 验收。全程约 1–3 分钟。</p>`;
    const box = el('div', '', body);
    const { close } = openModal({
      kicker: 'NEW ROUTE',
      title: '新增域名并加速',
      body: box,
      okLabel: '开始配置',
      onOk: async () => {
        const host = $('#f-host').value.trim().toLowerCase();
        const svc = $('#f-svc').value.trim();
        const err = (m) => { $('#f-host-err').textContent = m || ''; return false; };
        if (!/^(?=.{4,253}$)(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))+$/.test(host)) {
          return err('域名格式不合法');
        }
        if (!svc) return err('后端地址不能为空');
        $('#f-host-err').textContent = '';
        close();
        await runJob({ op: 'add', hostname: host, service: svc });
        return false;
      },
    });
    $$('[data-svc]', box).forEach((b) => b.addEventListener('click', () => {
      $('#f-svc').value = b.dataset.svc;
    }));
  }

  function openEditModal(host) {
    const route = S.state.routes.find((r) => r.hostname === host) || {};
    const body = el('div', '', `${fieldHtml({ label: `新后端 · ${host}`, id: 'f-svc', value: route.service || '', placeholder: 'http://127.0.0.1:8080' })}
      <p>只改隧道 ingress 指向，不动 DNS 与 SaaS。会重启一次隧道（约 20s）。</p>`);
    openModal({
      kicker: 'EDIT BACKEND',
      title: '改后端',
      body,
      okLabel: '保存并重启',
      onOk: async () => {
        const svc = $('#f-svc').value.trim();
        if (!svc) { $('#f-svc-err').textContent = '后端地址不能为空'; return false; }
        $('#modal').hidden = true;
        await runJob({ op: 'update', hostname: host, service: svc });
        return false;
      },
    });
  }

  function confirmDelete(host) {
    const body = el('div', '', `<p>这一步会同时删除：隧道 ingress、SaaS 自定义主机名、以及 <code>${esc(host)}</code> 在本账号 zone 下的解析记录。</p>
      ${fieldHtml({ label: '输入域名确认', id: 'f-confirm', placeholder: host })}
      <p>不可撤销，备份会写在 <code>~/.cloudflared/*.bak.*</code>。</p>`);
    openModal({
      kicker: 'DESTRUCTIVE',
      title: '删除这个域名',
      body,
      okLabel: '确认删除',
      danger: true,
      onOk: async () => {
        const typed = $('#f-confirm').value.trim().toLowerCase();
        if (typed !== host) { $('#f-confirm-err').textContent = '必须原样输入域名'; return false; }
        $('#modal').hidden = true;
        await runJob({ op: 'delete', hostname: host, confirm: host });
        return false;
      },
    });
  }

  function confirmSwitch(host) {
    const body = el('div', '', `<p>把 <code>${esc(host)}</code> 的入口 CNAME 切到优选入口
      <code>${esc(S.state.preferred.entry)}</code> → <code>${esc(S.state.preferred.target || '')}</code>。</p>
      <p>如果 SaaS 还没 active，会先补验证记录再切换，可能要等证书签发。</p>`);
    openModal({
      kicker: 'ROUTE TO ENTRY',
      title: '切到优选入口',
      body,
      okLabel: '切换',
      onOk: async () => {
        $('#modal').hidden = true;
        await runJob({ op: 'switch', hostname: host });
        return false;
      },
    });
  }

  async function openDetail(host) {
    const body = el('div', 'detail-body', '<p class="hint">正在读取云端状态…</p>');
    openModal({
      kicker: 'DETAIL', title: host, body, okLabel: '关闭',
      hideCancel: true, onOk: async () => true,
    });
    $('#modal-ok').className = 'btn btn-wire';

    const paint = (h, phase) => {
      const rows = [
        ['backend', h.service || '—'],
        ['zone', h.zone || 'external'],
        ['saas', h.saas ? `${h.saas.status} / ssl ${h.saas.ssl}` : '—'],
        ['dns', h.dns?.cname || (h.dns?.a ? `A ${h.dns.a}` : '—')],
        ['probe', h.probe ? (h.probe.ok ? 'UP' : 'DOWN') : '—'],
        ['https', h.https ? (h.https.ok ? 'PASS' : h.https.ok === false ? 'FAIL' : '…') : phase === 'fast' ? '检测中…' : '—'],
      ];
      body.innerHTML = `<div class="route-meta" style="gap:8px">${rows.map(([k, v]) => `<div><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join('')}</div>`
        + (h.saas?.errors?.length ? `<p>验证提示：${esc(h.saas.errors.join('; '))}</p>` : '')
        + (h.service ? `<p class="hint">后端探测：${esc(h.probe?.detail || '—')}</p>` : '')
        + (h.https?.detail ? `<pre>${esc(h.https.detail)}</pre>` : '');
    };

    try {
      const fast = await api(`/api/host/${encodeURIComponent(host)}`);
      paint(fast.host, 'fast');
      const slow = await api(`/api/host/${encodeURIComponent(host)}?https=1`);
      paint(slow.host, 'full');
    } catch (err) {
      body.innerHTML = `<p>读取失败：${esc(err.message)}</p>`;
    }
  }

  /* ── 命令面板 ─────────────────────────────────────────── */
  function commands() {
    const list = [
      { kind: 'act', label: '新增域名并加速', hint: 'A', run: () => openAddModal() },
      { kind: 'act', label: '全链路体检', hint: 'H', run: () => runHealth() },
      { kind: 'act', label: '刷新数据', hint: 'R', run: () => refreshAll() },
      { kind: 'act', label: '退出会话', hint: '', run: () => logout() },
    ];
    (S.state?.routes || []).forEach((r) => {
      list.push({ kind: 'route', label: r.hostname, hint: r.service || '', run: () => focusRoute(r.hostname, true) });
      list.push({ kind: 'edit', label: `改后端 · ${r.hostname}`, hint: 'update', run: () => openEditModal(r.hostname) });
      list.push({ kind: 'del', label: `删除 · ${r.hostname}`, hint: 'destructive', run: () => confirmDelete(r.hostname) });
    });
    S.services.filter((s) => !s.mapped.length).forEach((s) => {
      list.push({
        kind: 'svc', label: `映射 :${s.port} ${s.friendly}`, hint: 'unmapped',
        run: () => openAddModal(`http://127.0.0.1:${s.port}`),
      });
    });
    return list;
  }

  function openPalette() {
    const pal = $('#palette');
    pal.hidden = false;
    const input = $('#palette-input');
    input.value = '';
    S.paletteItems = commands().slice(0, 9);
    S.paletteIdx = 0;
    renderPalette();
    input.focus();
  }

  function renderPalette() {
    const box = $('#palette-list');
    box.innerHTML = S.paletteItems.map((item, i) => `<li class="${i === S.paletteIdx ? 'sel' : ''}" data-i="${i}" role="option">
      <span class="kind">${esc(item.kind)}</span>
      <span class="label">${esc(item.label)}</span>
      <span class="hint">${esc(item.hint || '')}</span>
    </li>`).join('');
    $$('li', box).forEach((li) => {
      li.addEventListener('click', () => execPalette(Number(li.dataset.i)));
      li.addEventListener('pointerenter', () => { S.paletteIdx = Number(li.dataset.i); renderPalette(); });
    });
  }

  function filterPalette(q) {
    const query = q.trim().toLowerCase();
    const all = commands();
    S.paletteItems = (query
      ? all.filter((c) => `${c.kind} ${c.label} ${c.hint}`.toLowerCase().includes(query))
      : all
    ).slice(0, 40);
    S.paletteIdx = 0;
    renderPalette();
  }

  function execPalette(i) {
    const item = S.paletteItems[i];
    if (!item) return;
    $('#palette').hidden = true;
    item.run();
  }

  function initPalette() {
    $('#cmd-open').addEventListener('click', openPalette);
    const input = $('#palette-input');
    input.addEventListener('input', (e) => filterPalette(e.target.value));
    input.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowDown') { S.paletteIdx = Math.min(S.paletteItems.length - 1, S.paletteIdx + 1); renderPalette(); e.preventDefault(); }
      if (e.key === 'ArrowUp') { S.paletteIdx = Math.max(0, S.paletteIdx - 1); renderPalette(); e.preventDefault(); }
      if (e.key === 'Enter') { execPalette(S.paletteIdx); e.preventDefault(); }
      if (e.key === 'Escape') { $('#palette').hidden = true; }
    });
    $('#palette').addEventListener('click', (e) => { if (e.target.id === 'palette') $('#palette').hidden = true; });
  }

  /* ── 拓扑画布 ─────────────────────────────────────────── */
  const Topology = (() => {
    let canvas, ctx, W = 0, H = 0, dpr = 1, raf = 0, t0 = performance.now();
    let nodes = [], edges = [], dots = [], hoverDot = null, dragNode = null;
    let pointer = { x: 0, y: 0 }, inside = false;

    const NODE_R = 5;

    function resize() {
      if (!canvas) return;
      const rect = canvas.getBoundingClientRect();
      W = Math.max(320, rect.width);
      H = Math.max(240, rect.height);
      dpr = Math.min(2, window.devicePixelRatio || 1);
      canvas.width = Math.round(W * dpr);
      canvas.height = Math.round(H * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      layout();
    }

    function layout() {
      const d = S.state;
      if (!d) return;
      const prev = new Map(nodes.map((n) => [n.id, n]));
      nodes = [];
      edges = [];
      dots = [];

      const mk = (id, label, sub, kind, x, y, r = NODE_R) => {
        const old = prev.get(id);
        return {
          id, label, sub, kind, x, y, r,
          hx: x, hy: y,
          vx: old ? old.vx || 0 : 0,
          vy: old ? old.vy || 0 : 0,
          x0: old ? old.x : x,
          y0: old ? old.y : y,
        };
      };

      const padL = Math.min(120, W * 0.14);
      const entryX = padL + 40;
      const saasX = entryX + (W - entryX) * 0.26;
      const tunnelX = entryX + (W - entryX) * 0.52;
      const svcX = W - Math.min(96, W * 0.12);

      nodes.push(mk('entry', 'ENTRY', d.preferred.target || d.preferred.entry, 'entry', entryX, H * 0.5, 7));
      nodes.push(mk('saas', 'SAAS', d.fallback, 'saas', saasX, H * 0.5, 6));
      nodes.push(mk('tunnel', 'TUNNEL', `${d.service.name.split('.')[0]}`, 'tunnel', tunnelX, H * 0.5, 6));

      const map = new Map(nodes.map((n) => [n.id, n]));
      edges.push({ a: map.get('entry'), b: map.get('saas'), speed: 0.16 });
      edges.push({ a: map.get('saas'), b: map.get('tunnel'), speed: 0.13 });

      // 后端节点：按端口去重
      const seen = new Map();
      (d.routes || []).forEach((r) => {
        const m = /:(\d+)\s*$/.exec(r.service || '');
        const port = m ? m[1] : r.service;
        if (!seen.has(port)) seen.set(port, []);
        seen.get(port).push(r.hostname);
      });
      const ports = Array.from(seen.keys()).slice(0, 12);
      ports.forEach((port, i) => {
        const y = H * 0.14 + (H * 0.72) * (ports.length === 1 ? 0.5 : i / (ports.length - 1));
        const svc = S.services.find((s) => String(s.port) === String(port));
        const up = svc?.probe ? svc.probe.ok : true;
        const node = mk(`svc-${port}`, port, `${seen.get(port).length} hosts`, up ? 'svc' : 'svc-down', svcX, y, 4);
        nodes.push(node);
        edges.push({ a: map.get('tunnel'), b: node, speed: 0.1 + (up ? 0.06 : 0) });
      });

      // 域名点：以 ENTRY 为圆心的一圈扇形
      const routes = d.routes || [];
      const baseR = Math.max(42, Math.min(96, W * 0.085));
      routes.forEach((r, i) => {
        const k = routes.length === 1 ? 0.5 : i / (routes.length - 1);
        // 扇形放在 ENTRY 左侧，避免压住节点标签
        const ang = Math.PI * 0.52 + k * Math.PI * 0.96;
        const rr = baseR + (i % 3) * 9;
        const dot = {
          host: r.hostname,
          service: r.service,
          saas: r.saas,
          ang,
          rr,
          x: entryX + Math.cos(ang) * rr,
          y: H * 0.5 + Math.sin(ang) * rr * 0.92,
          r: 2.1,
        };
        dots.push(dot);
      });

      S.target = S.target || null;
    }

    function drawFrame(now) {
      const t = (now - t0) / 1000;
      ctx.clearRect(0, 0, W, H);

      // 指针视差
      const px = inside ? (pointer.x / W - 0.5) : 0;
      const py = inside ? (pointer.y / H - 0.5) : 0;

      // 域名扇形
      const entry = nodes.find((n) => n.id === 'entry');
      dots.forEach((dot) => {
        if (!entry) return;
        const isTarget = S.target === dot.host;
        const isHover = hoverDot === dot;
        const alpha = isTarget || isHover ? 0.85 : 0.2;
        ctx.strokeStyle = `rgba(236,231,219,${alpha * 0.34})`;
        ctx.lineWidth = 0.6;
        ctx.beginPath();
        ctx.moveTo(entry.x, entry.y);
        ctx.lineTo(dot.x, dot.y);
        ctx.stroke();
        const col = isTarget || isHover ? '200,255,60'
          : dot.saas && dot.saas.ssl && dot.saas.ssl !== 'active' ? '255,182,56'
          : '236,231,219';
        ctx.fillStyle = `rgba(${col},${isTarget || isHover ? 1 : 0.5})`;
        ctx.beginPath();
        ctx.arc(dot.x, dot.y, isTarget || isHover ? dot.r + 1.2 : dot.r, 0, Math.PI * 2);
        ctx.fill();
      });

      // 边 + 脉冲
      edges.forEach((e, ei) => {
        if (!e.a || !e.b) return;
        const a = e.a, b = e.b;
        ctx.strokeStyle = 'rgba(236,231,219,0.13)';
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        const midx = (a.x + b.x) / 2;
        ctx.bezierCurveTo(midx, a.y, midx, b.y, b.x, b.y);
        ctx.stroke();

        if (reduceMotion) return;
        for (let k = 0; k < 3; k += 1) {
          const p = ((t * e.speed * 2.4) + k / 3 + ei * 0.07) % 1;
          const mx = (a.x + b.x) / 2;
          const x = bez(a.x, mx, mx, b.x, p);
          const y = bez(a.y, a.y, b.y, b.y, p);
          const fade = Math.sin(p * Math.PI);
          ctx.fillStyle = `rgba(200,255,60,${0.16 + fade * 0.5})`;
          ctx.beginPath();
          ctx.arc(x, y, 1.5 + fade * 1.1, 0, Math.PI * 2);
          ctx.fill();
        }
      });

      // 节点
      nodes.forEach((n) => {
        const ox = n.x + px * (n.kind === 'entry' ? 10 : 6);
        const oy = n.y + py * (n.kind === 'entry' ? 10 : 6);
        const entryKind = n.kind === 'entry';
        const down = n.kind === 'svc-down';
        ctx.strokeStyle = entryKind ? 'rgba(200,255,60,0.85)'
          : down ? 'rgba(255,75,43,0.7)'
          : 'rgba(236,231,219,0.45)';
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.arc(ox, oy, n.r + 3, 0, Math.PI * 2);
        ctx.stroke();

        ctx.fillStyle = entryKind ? '#c8ff3c' : down ? '#ff4b2b' : 'rgba(236,231,219,0.8)';
        ctx.beginPath();
        ctx.arc(ox, oy, n.r * 0.62, 0, Math.PI * 2);
        ctx.fill();

        if (entryKind && !reduceMotion) {
          const pulse = (Math.sin(t * 2) + 1) / 2;
          ctx.strokeStyle = `rgba(200,255,60,${0.28 * (1 - pulse)})`;
          ctx.beginPath();
          ctx.arc(ox, oy, n.r + 3 + pulse * 16, 0, Math.PI * 2);
          ctx.stroke();
        }

        // 标签
        ctx.textBaseline = 'middle';
        ctx.font = '600 9px "Noto Sans Mono", ui-monospace, monospace';
        ctx.fillStyle = entryKind ? 'rgba(200,255,60,.95)' : 'rgba(236,231,219,.72)';
        const labelRight = n.kind === 'svc' || n.kind === 'svc-down';
        const lx = labelRight ? ox + 10 : ox;
        const ly = labelRight ? oy : oy - n.r - 15;
        ctx.textAlign = labelRight ? 'left' : 'center';
        ctx.fillText(String(n.label).toUpperCase(), lx, ly);
        // 窄屏就别塞副标题了，主轴三个节点的字会互相压住
        const showSub = !labelRight && (W >= 560 || entryKind);
        if (n.sub && (showSub || labelRight)) {
          ctx.font = '400 8px "Noto Sans Mono", ui-monospace, monospace';
          ctx.fillStyle = 'rgba(236,231,219,.34)';
          const raw = String(n.sub);
          const limit = W >= 560 ? 28 : 18;
          const sub = raw.length > limit ? `${raw.slice(0, limit - 1)}…` : raw;
          ctx.fillText(sub, labelRight ? lx : ox, labelRight ? ly + 11 : oy + n.r + 15);
        }
      });

      // hover 提示
      if (hoverDot) {
        const pad = 6;
        ctx.font = '500 9.5px "Noto Sans Mono", ui-monospace, monospace';
        const text = hoverDot.host;
        const w = ctx.measureText(text).width + pad * 2;
        const hx = Math.min(W - w - 6, Math.max(6, hoverDot.x + 10));
        const hy = Math.min(H - 26, Math.max(6, hoverDot.y - 20));
        ctx.fillStyle = 'rgba(8,9,11,.92)';
        ctx.fillRect(hx, hy, w, 18);
        ctx.strokeStyle = 'rgba(200,255,60,.5)';
        ctx.lineWidth = 1;
        ctx.strokeRect(hx + 0.5, hy + 0.5, w - 1, 17);
        ctx.fillStyle = '#ece7db';
        ctx.textAlign = 'left';
        ctx.textBaseline = 'middle';
        ctx.fillText(text, hx + pad, hy + 9);
      }
    }

    function bez(p0, p1, p2, p3, t) {
      const mt = 1 - t;
      return mt * mt * mt * p0 + 3 * mt * mt * t * p1 + 3 * mt * t * t * p2 + t * t * t * p3;
    }

    function step(now) {
      // 弹簧回位
      const k = 0.012, damp = 0.9;
      nodes.forEach((n) => {
        if (dragNode === n) return;
        n.vx = (n.vx + (n.hx - n.x) * k) * damp;
        n.vy = (n.vy + (n.hy - n.y) * k) * damp;
        n.x += n.vx;
        n.y += n.vy;
      });
      dots.forEach((dot) => {
        const e = nodes.find((n) => n.id === 'entry');
        if (!e) return;
        dot.x = e.x + Math.cos(dot.ang) * dot.rr;
        dot.y = e.y + Math.sin(dot.ang) * dot.rr * 0.92;
      });
      drawFrame(now);
      raf = requestAnimationFrame(step);
    }

    function hit(ev) {
      const rect = canvas.getBoundingClientRect();
      const x = ev.clientX - rect.left;
      const y = ev.clientY - rect.top;
      let best = null;
      dots.forEach((d) => {
        if (Math.hypot(d.x - x, d.y - y) < 9) best = d;
      });
      return { dot: best, x, y };
    }

    function nodeAt(x, y) {
      return nodes.find((n) => Math.hypot(n.x - x, n.y - y) < n.r + 10) || null;
    }

    function init(node) {
      canvas = node;
      if (!canvas) return;
      ctx = canvas.getContext('2d');
      const ro = new ResizeObserver(() => resize());
      ro.observe(canvas);
      canvas.addEventListener('pointermove', (ev) => {
        const rect = canvas.getBoundingClientRect();
        pointer = { x: ev.clientX - rect.left, y: ev.clientY - rect.top };
        inside = true;
        if (dragNode) {
          dragNode.x = pointer.x;
          dragNode.y = pointer.y;
        }
        const h = hit(ev);
        hoverDot = h.dot;
        canvas.style.cursor = dragNode ? 'grabbing' : h.dot ? 'pointer' : nodeAt(h.x, h.y) ? 'grab' : 'default';
      });
      canvas.addEventListener('pointerleave', () => { inside = false; hoverDot = null; dragNode = null; });
      canvas.addEventListener('pointerdown', (ev) => {
        const h = hit(ev);
        const n = nodeAt(h.x, h.y);
        if (n) {
          dragNode = n;
          canvas.setPointerCapture?.(ev.pointerId);
        }
      });
      canvas.addEventListener('pointerup', () => { dragNode = null; });
      canvas.addEventListener('click', (ev) => {
        const h = hit(ev);
        if (h.dot) focusRoute(h.dot.host, false);
      });
      resize();
      if (raf) cancelAnimationFrame(raf);
      raf = requestAnimationFrame(step);
    }

    return { init, relayout: () => { layout(); } };
  })();

  /* ── 数据加载 ─────────────────────────────────────────── */
  async function loadState() {
    const data = await api('/api/state');
    S.state = data;
    renderStats();
    renderRoutes();
    Topology.relayout();
  }

  async function loadServices(withProbe = true) {
    const box = $('#listener-list');
    if (!S.services.length) {
      box.innerHTML = '<div class="skeleton-row"></div><div class="skeleton-row"></div>';
      box.insertAdjacentHTML('afterbegin', '<p class="loading-note">扫描本机监听端口并逐个探活…</p>');
    }
    const data = await api(`/api/services?probe=${withProbe ? 1 : 0}`);
    S.services = data.services || [];
    renderListeners();
    renderTicker();
    Topology.relayout();
    const mapped = S.services.filter((x) => x.mapped.length).length;
    setCount('count-services', `${mapped}/${S.services.length}`);
  }

  function setCount(id, value) {
    const node = document.getElementById(id);
    if (node) node.textContent = value;
  }

  async function loadJournal() {
    const data = await api('/api/audit?limit=40');
    S.journal = data.rows || [];
    renderJournal();
  }

  async function refreshAll() {
    await Promise.all([loadState(), loadServices(true), loadJournal()]);
    renderTicker();
    toast('已刷新', 'state / services / audit');
  }

  /* ── 会话 ─────────────────────────────────────────────── */
  async function boot() {
    document.body.classList.remove('is-gated');
    $('#gate').classList.add('out');
    setTimeout(() => { $('#gate').hidden = true; }, 700);
    $('#app').hidden = false;
    initReveal();
    initRail();
    initCursor();
    Topology.init($('#topology'));
    const results = await Promise.allSettled([loadState(), loadServices(true), loadJournal()]);
    const failed = results.filter((r) => r.status === 'rejected');
    if (failed.length) {
      failed.forEach((f) => toast('部分数据加载失败', f.reason?.message || '', 'warn'));
    }
    renderTicker();
  }

  async function logout() {
    try { await api('/api/session', { method: 'DELETE' }); } catch (_) { /* ignore */ }
    location.reload();
  }

  async function submitToken() {
    const input = $('#token');
    const msg = $('#gate-msg');
    const token = input.value.trim();
    if (!token) { msg.textContent = '请输入凭据'; return; }
    msg.textContent = '';
    msg.classList.remove('ok');
    const btn = $('#gate-submit');
    btn.disabled = true;
    try {
      const res = await api('/api/session', { method: 'POST', body: { token } });
      S.csrf = res.csrf || readCookie('arc_csrf');
      sessionStorage.setItem('arc-hint', '1');
      msg.textContent = '验证通过，正在载入…';
      msg.classList.add('ok');
      setTimeout(() => boot(), 220);
    } catch (err) {
      msg.textContent = err.message + (err.data?.retry_after ? `（${err.data.retry_after}s 后可再试）` : '');
      $('#gate').classList.remove('shake');
      void $('#gate').offsetWidth;
      $('#gate').classList.add('shake');
      input.select();
    } finally {
      btn.disabled = false;
    }
  }

  function initGate() {
    document.body.classList.add('is-gated');
    $('#gate-submit').addEventListener('click', submitToken);
    $('#token').addEventListener('keydown', (e) => { if (e.key === 'Enter') submitToken(); });
    (async () => {
      // 刷新页面时 cookie 还在就直接进；csrf 来自可读的 arc_csrf 双提交 cookie。
      const s = await api('/api/session').catch(() => null);
      if (s && s.authenticated) {
        S.csrf = S.csrf || readCookie('arc_csrf');
        boot();
        return;
      }
      setTimeout(() => $('#token').focus(), 500);
    })();
  }

  /* ── 全局事件 ─────────────────────────────────────────── */
  function initEvents() {
    $('#hero-add').addEventListener('click', () => openAddModal());
    $('#hero-health').addEventListener('click', () => runHealth());
    $('#diag-run').addEventListener('click', () => runHealth());
    $('#services-refresh').addEventListener('click', () => loadServices(true).then(() => toast('已刷新', 'listeners')));
    $('#routes-refresh').addEventListener('click', () => loadState().then(() => toast('已刷新', 'routes')));
    $('#journal-refresh').addEventListener('click', () => loadJournal().then(() => toast('已刷新', 'journal')));
    $('#logout').addEventListener('click', logout);
    $('#jobbar-toggle').addEventListener('click', (e) => {
      const bar = $('#jobbar');
      bar.classList.toggle('open');
      e.currentTarget.textContent = bar.classList.contains('open') ? '收起' : '展开';
      e.currentTarget.setAttribute('aria-expanded', bar.classList.contains('open'));
    });

    $('#services-filter').addEventListener('input', (e) => {
      S.filter.services = e.target.value; renderListeners();
    });
    $$('#services-seg button').forEach((b) => b.addEventListener('click', () => {
      S.filter.scope = b.dataset.scope;
      $$('#services-seg button').forEach((x) => x.classList.toggle('on', x === b));
      renderListeners();
    }));
    $('#routes-filter').addEventListener('input', (e) => {
      S.filter.routes = e.target.value; renderRoutes();
    });

    window.addEventListener('keydown', (e) => {
      const typing = /input|textarea|select/i.test(document.activeElement?.tagName || '');
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        if ($('#app').hidden) return;
        if ($('#palette').hidden) openPalette(); else $('#palette').hidden = true;
        return;
      }
      if (e.key === 'Escape') {
        if (!$('#palette').hidden) { $('#palette').hidden = true; return; }
        if (!$('#modal').hidden) { $('#modal')._close?.(); return; }
      }
      if (typing || $('#app').hidden) return;
      if (e.key.toLowerCase() === 'a') openAddModal();
      if (e.key.toLowerCase() === 'h') runHealth();
      if (e.key.toLowerCase() === 'r') refreshAll();
    });
  }

  /* ── 启动 ─────────────────────────────────────────────── */
  document.addEventListener('DOMContentLoaded', () => {
    initGate();
    initEvents();
    initPalette();
    initReveal();
  });
})();

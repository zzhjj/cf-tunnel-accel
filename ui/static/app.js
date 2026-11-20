/* 原生同源控制台。读取分区独立；写请求不重放；任务与审计保持全局作用域。 */
(() => {
  'use strict';
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const PROFILE_KEY = 'arc-profile';
  const JOB_KEY = 'arc-last-job';
  const TIMEOUT = 12000;
  const S = {
    authenticated: false, authEpoch: 0, epoch: 0, profiles: [], profile: null, csrf: '',
    controllers: new Set(), parts: {}, state: null, services: null, cdn: null, environment: null,
    ranked: null, jobs: new Map(), selectedJob: null, cursor: 0, logs: [], omitted: 0,
    detailGeneration: 0, detailLost: false, detailDirty: false, submitting: false, uncertain: false,
    offline: !navigator.onLine,
    jobsSupported: false, jobsLost: false, tickBusy: false, jobsLoaded: false,
  };
  const stored = (storage, key) => { try { return storage.getItem(key); } catch (_) { return null; } };
  const store = (storage, key, value) => { try { value == null ? storage.removeItem(key) : storage.setItem(key, value); } catch (_) { /* optional preference */ } };
  const cookie = () => { const match = document.cookie.match(/(?:^|; )arc_csrf=([^;]*)/); return match ? decodeURIComponent(match[1]) : ''; };
  const isPending = job => ['queued', 'running'].includes(job?.status);
  const profileName = id => S.profiles.find(p => p.id === id)?.name || id || '归属未提供';
  const timeText = value => {
    if (value == null || value === '') return '未提供';
    const date = new Date(typeof value === 'number' ? value * 1000 : value);
    return Number.isFinite(date.getTime()) ? date.toLocaleString('zh-CN', {hour12: false}) : String(value);
  };
  const status = (text, kind = 'pending') => `<span class="status ${kind}">${esc(text)}</span>`;
  const obsolete = () => Object.assign(new Error('响应已过期'), {name: 'StaleResponse'});
  const quiet = err => err.name === 'StaleResponse';
  function message(text) {
    const node = document.createElement('p'); node.textContent = text;
    $('#messages').replaceChildren(node);
    setTimeout(() => node.remove(), 8000);
  }
  function openGate(text = '') {
    const wasAuthenticated = S.authenticated;
    S.authenticated = false;
    if (wasAuthenticated) {
      S.authEpoch++;
      S.controllers.forEach(r => r.controller.abort());
    }
    $('#app').inert = true;
    $('#token').value = '';
    $('#gate-msg').textContent = text;
    $('#api-status').textContent = 'API 需登录';
    $('#api-status').className = 'status pending';
    if (!$('#gate').open) $('#gate').showModal();
    $('#token').focus();
  }
  async function api(path, {method = 'GET', body} = {}) {
    const url = new URL(path, location.origin);
    const global = ['/api/profiles', '/api/session', '/api/audit'].includes(url.pathname)
      || (method === 'GET' && /^\/api\/jobs(?:\/|$)/.test(url.pathname));
    const epoch = S.epoch, authEpoch = S.authEpoch;
    if (!global) {
      if (!S.profile) throw new Error('请先选择配置');
      url.searchParams.set('profile', S.profile);
    }
    const controller = new AbortController();
    const record = {controller, scoped: !global};
    S.controllers.add(record);
    let timedOut = false;
    const timer = setTimeout(() => { timedOut = true; controller.abort(); }, TIMEOUT);
    try {
      const headers = {'Accept': 'application/json'};
      if (body !== undefined) headers['Content-Type'] = 'application/json';
      if (method !== 'GET' && (S.csrf || cookie())) headers['X-CSRF'] = S.csrf || cookie();
      const res = await fetch(url.pathname + url.search, {
        method, headers, body: body === undefined ? undefined : JSON.stringify(body),
        credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
      });
      let data = {}, malformed = false;
      if (res.status !== 204) {
        try { data = await res.json(); } catch (_) { malformed = true; }
      }
      if ((!global && epoch !== S.epoch) || authEpoch !== S.authEpoch) throw obsolete();
      if (res.status === 401 && url.pathname !== '/api/session') openGate('会话已过期，请重新登录。写请求不会自动重放。');
      if (malformed) throw Object.assign(new Error(`HTTP ${res.status}：响应不是有效 JSON`), {status:res.status, rejected:!res.ok});
      if (!res.ok || data.ok === false) {
        let text = data.error || `HTTP ${res.status}`;
        if (res.status === 429) text += `；请等待 ${data.retry_after || res.headers.get('Retry-After') || '数'} 秒后重试`;
        throw Object.assign(new Error(text), {status: res.status, data, rejected: true});
      }
      return data;
    } catch (err) {
      if (err.status === 401) throw err;
      if ((!global && epoch !== S.epoch) || authEpoch !== S.authEpoch) throw obsolete();
      if (timedOut) throw Object.assign(new Error('请求超时（12 秒），请重试读取；写请求状态须先同步'), {ambiguous: method !== 'GET'});
      if (err.name === 'AbortError') throw obsolete();
      if (!err.rejected && method !== 'GET') err.ambiguous = true;
      throw err;
    } finally { clearTimeout(timer); S.controllers.delete(record); }
  }

  /* 每个分区有自己的请求代次，不使用全局“全部就绪”锁住界面。 */
  function notice(part, text, kind = '') {
    const node = $(`#${part === 'state' ? 'routes' : part}-notice`);
    node.textContent = text; node.className = `notice ${kind}`;
  }
  function errorsText(errors) {
    if (!errors) return '';
    if (Array.isArray(errors)) return errors.map(e => typeof e === 'object' ? [e.component || e.name, e.error || e.message || e.reason].filter(Boolean).join('：') : String(e)).join('；');
    if (typeof errors === 'object') return Object.entries(errors).map(([k,v]) => `${k}：${typeof v === 'object' ? v.message || v.error || '检查失败' : v}`).join('；');
    return String(errors);
  }
  async function loadPart(part, {probe = false, fresh = false} = {}) {
    if (!S.authenticated || !S.profile) return;
    const scoped = !['jobs', 'audit'].includes(part), epoch = S.epoch;
    const previous = S.parts[part] || {};
    const version = (previous.version || 0) + 1;
    S.parts[part] = {...previous, version, loading: true, error: null};
    notice(part, previous.updated ? '正在刷新；下方为上次读取的数据' : '正在加载…');
    updateMeta(); updateControls();
    try {
      const paths = {state: `/api/state${fresh ? '?fresh=1' : ''}`, services: `/api/services?probe=${probe ? 1 : 0}`, cdn: '/api/cdn', environment: '/api/environment', audit: '/api/audit?limit=100', jobs: '/api/jobs'};
      const data = await api(paths[part]);
      if ((scoped && epoch !== S.epoch) || S.parts[part]?.version !== version) return;
      if (part === 'state') {
        if (!Array.isArray(data.routes) || !data.service || !data.preferred) throw new Error('路由状态格式错误');
        S.state = data; renderState();
      } else if (part === 'services') {
        if (!Array.isArray(data.services)) throw new Error('服务列表格式错误');
        S.services = data.services; renderServices();
      } else if (part === 'cdn') {
        if (!data.cdn || !Array.isArray(data.cdn.candidates)) throw new Error('优选状态格式错误');
        S.cdn = data.cdn; renderCdn();
      } else if (part === 'environment') {
        if (!data.environment || typeof data.environment.ready !== 'boolean') throw new Error('环境诊断格式错误');
        S.environment = data.environment; renderEnvironment();
      } else if (part === 'audit') {
        if (!Array.isArray(data.rows)) throw new Error('审计列表格式错误');
        renderAudit(data.rows);
      } else { acceptJobList(data); }
      const error = errorsText(part === 'state' ? data.errors : part === 'environment' ? data.environment.errors : part === 'cdn' ? data.cdn.errors : null);
      const warning = [errorsText(data.warnings || (part === 'environment' ? data.environment.warnings : null)), data.store_error ? `任务记录存储异常：${data.store_error}；刷新后任务恢复可能不完整` : ''].filter(Boolean).join('；');
      const updated = part === 'state' ? data.updated_at : null;
      S.parts[part] = {version, loading: false, updated: Date.now(), serverUpdated: updated, error: error || null, partial: Boolean(error)};
      if (part === 'state' && S.state) renderState();
      if (part === 'environment' && S.state) renderState();
      if (part === 'environment' && !S.environment.ready) notice(part, '环境未就绪：请处理下方缺失项后重试', 'warning');
      else notice(part, error ? `部分数据未确认：${error}` : warning ? `检查提示：${warning}；读取时间：${timeText(updated || Date.now() / 1000)}` : `读取时间：${timeText(updated || Date.now() / 1000)}`, error ? 'error' : warning ? 'warning' : '');
    } catch (err) {
      if (quiet(err) || (scoped && epoch !== S.epoch) || S.parts[part]?.version !== version) return;
      S.parts[part] = {...S.parts[part], loading: false, error: err.message, partial: false};
      const unavailable = ['environment', 'jobs'].includes(part) && [404,405].includes(err.status);
      notice(part, unavailable ? `当前后端未提供${part === 'jobs' ? '任务列表' : '环境诊断'}接口；请更新配套后端后重试。${part === 'environment' ? '可在相同配置环境执行 cf-tunnel-accel env。' : '已知任务仍可按 ID 同步。'}` : `读取失败：${err.message}${previous.updated ? '；下方为旧数据，未确认当前状态' : ''}`, 'error');
      if (part === 'jobs') { S.jobsLost = true; S.jobsSupported = false; renderJobs(); }
      if (part === 'cdn') renderCdn();
      if (part === 'state' && S.state) renderState();
    }
    updateMeta(); updateControls();
  }
  function updateMeta() {
    const parts = Object.values(S.parts);
    const failed = parts.some(p => p.error);
    $('#api-status').textContent = !S.authenticated ? 'API 需登录' : S.offline ? 'API 连接中断' : failed ? 'API 部分读取失败' : parts.some(p => p.loading) ? 'API 正在读取' : 'API 已连接';
    $('#api-status').className = `status ${failed ? 'bad' : 'pending'}`;
    const state = S.parts.state;
    $('#updated-at').textContent = state?.updated ? `路由更新：${timeText(state.serverUpdated || state.updated / 1000)}` : '路由尚未更新';
    let serverTime = state?.serverUpdated ? new Date(typeof state.serverUpdated === 'number' ? state.serverUpdated * 1000 : state.serverUpdated).getTime() : state?.updated;
    const stale = S.offline || !state?.updated || state.error || state.loading || Date.now() - serverTime > 60000;
    $('#stale-state').textContent = state?.error ? '路由状态未确认' : stale ? '路由数据待更新' : '路由数据已同步';
    $('#stale-state').className = `status ${stale ? 'pending' : 'good'}`;
  }
  function writeReady() {
    const readiness = S.state?.readiness;
    const statePart = S.parts.state;
    const readFailed = statePart?.error && !statePart?.partial;
    return S.authenticated && !!S.profile && !!S.state && !readFailed && !statePart?.loading
      && !S.state.errors?.config && readiness !== false && readiness?.ready !== false && S.environment?.ready !== false;
  }
  function busy() { return S.submitting || S.uncertain || [...S.jobs.values()].some(isPending); }
  function updateControls() {
    $('#route-add').disabled = !writeReady() || busy();
    const cdnReady = S.authenticated && !!S.cdn && !(S.parts.cdn?.error && !S.parts.cdn?.partial) && !S.parts.cdn?.loading;
    $('#cdn-test').disabled = !S.profile || !S.authenticated || busy() || !cdnReady;
    $('#cdn-best').disabled = !writeReady() || !cdnReady || busy();
    $('#cdn-manual-go').disabled = !writeReady() || !cdnReady || busy();
    $('#diag-run').disabled = !S.profile || !S.authenticated || busy();
    $$('[data-route-action]').forEach(b => { b.disabled = busy() || (b.dataset.routeAction === 'health' ? !S.authenticated : !writeReady()); });
    $$('[data-bind]').forEach(b => { b.disabled = !writeReady() || busy(); });
    $$('[data-cdn-set]').forEach(b => { b.disabled = !writeReady() || !cdnReady || busy() || b.dataset.current === 'true'; });
    renderBanner();
  }
  async function loadProfiles() {
    const authEpoch = S.authEpoch;
    $('#profile-status').textContent = '正在加载配置列表…'; $('#profile-retry').hidden = true;
    try {
      const data = await api('/api/profiles');
      if (authEpoch !== S.authEpoch) return;
      if (!Array.isArray(data.profiles) || !data.profiles.length || data.profiles.some(p => !p?.id || typeof p.id !== 'string' || typeof p.name !== 'string') || new Set(data.profiles.map(p=>p.id)).size !== data.profiles.length) throw new Error('配置列表为空或格式错误');
      S.profiles = data.profiles;
      $('#profile-select').innerHTML = S.profiles.map(p => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('');
      $('#profile-select').disabled = false;
      const saved = stored(localStorage, PROFILE_KEY);
      const valid = id => S.profiles.some(p => p.id === id);
      const id = valid(S.profile) ? S.profile : valid(saved) ? saved : valid(data.default) ? data.default : S.profiles[0].id;
      selectProfile(id);
    } catch (err) {
      if (quiet(err)) return;
      $('#profile-status').textContent = `配置加载失败：${err.message}`; $('#profile-retry').hidden = false;
    }
  }
  function selectProfile(id) {
    if (!S.profiles.some(p => p.id === id)) return;
    if ($('#modal').open) $('#modal').close();
    S.epoch++;
    S.controllers.forEach(r => { if (r.scoped) r.controller.abort(); });
    S.profile = id; $('#profile-select').value = id; store(localStorage, PROFILE_KEY, id);
    S.state = S.services = S.cdn = S.environment = S.ranked = null;
    ['state', 'services', 'cdn', 'environment'].forEach(p => { delete S.parts[p]; });
    ['state-summary','route-grid','listener-list','cdn-summary','cdn-candidates','environment-content'].forEach(id => $(`#${id}`).replaceChildren());
    $('#route-count').textContent = ''; $('#cdn-manual').value = ''; $('#cdn-manual-error').textContent = '';
    $('#diag-result').textContent = '尚未执行检查';
    $('#profile-status').textContent = `当前配置：${profileName(id)}；各区域独立读取，任务与审计为全局。`;
    refreshScoped();
  }
  function refreshScoped() { return Promise.allSettled(['state','services','cdn','environment'].map(p => loadPart(p))); }
  function renderState() {
    const d = S.state, preferred = d.preferred;
    const verified = preferred.verified === true && !S.parts.state?.error;
    const service = d.service;
    const stateFailed = !!S.parts.state?.error;
    const serviceText = stateFailed ? '状态未确认' : service.active === true ? '运行中' : service.active === false ? '未运行' : '状态未知';
    const readiness = d.readiness;
    const reportedReady = typeof readiness === 'boolean' ? readiness : readiness?.ready;
    const ready = (!S.parts.state?.partial && S.parts.state?.error) || d.errors?.config || S.environment?.ready === false ? false : reportedReady;
    $('#state-summary').innerHTML = [
      ['路由数量', d.counts?.routes === null || d.errors?.config ? '未确认' : `${d.routes.length}${stateFailed ? '（上次读取）' : ''}`],
      ['Tunnel 服务', `${esc(service.name || '未提供')} ${status(serviceText, service.active === true && !stateFailed ? 'good' : 'pending')}`],
      ['优选目标', preferred.target && verified ? `${esc(preferred.target)} ${status('已验证','good')}` : `${esc(preferred.target || '未知')} ${status('未验证')}`],
      ['写入准备', status(ready === true ? '已就绪' : ready === false ? '未就绪' : '后端未提供就绪检查', ready === true ? 'good' : 'pending')],
    ].map(([title,value])=>`<div class="card summary"><span class="muted">${title}</span><div>${value}</div></div>`).join('');
    const reasons = errorsText(readiness?.missing || readiness?.errors);
    if (reasons) $('#state-summary').insertAdjacentHTML('beforeend', `<p class="notice warning">${esc(reasons)}；在环境页查看恢复步骤。</p>`);
    renderRoutes();
  }
  function renderRoutes() {
    if (!S.state) return;
    const query = $('#routes-filter').value.toLowerCase();
    const rows = S.state.routes.filter(r => [r.hostname,r.service,r.zone,r.external ? '外部' : ''].join(' ').toLowerCase().includes(query));
    $('#route-count').textContent = `${rows.length} / ${S.state.routes.length} 条`;
    $('#route-grid').innerHTML = rows.length ? rows.map(r => `<article class="route card" data-host="${esc(r.hostname)}">
      <div class="row-head"><h2>${esc(r.hostname)}</h2>${status(r.external === true ? '外部 DNS · 手动' : r.external === false ? r.zone || 'zone 未确认' : 'zone 归属未确认')}</div>
      <dl><dt>本机后端</dt><dd>${esc(r.service || '未提供')}</dd><dt>源站 TLS</dt><dd>${r.service?.startsWith('https://') ? r.no_tls_verify ? '跳过证书校验' : '校验证书（不等同于异常）' : 'HTTP / 不适用'}</dd><dt>SaaS / SSL</dt><dd>${r.saas ? esc(`${r.saas.status || '未知'} / ${r.saas.ssl || '未知'}`) : '未确认'}</dd></dl>
      <div class="actions"><button type="button" data-route-action="edit">修改后端</button><button type="button" data-route-action="switch">切到优选…</button><button type="button" data-route-action="health">检查</button><button type="button" data-detail="${esc(r.hostname)}">详情</button><button type="button" class="danger" data-route-action="delete">删除…</button></div></article>`).join('') : `<p class="empty">${S.state.errors?.config ? '路由配置读取失败，路由数量未确认。请修复配置后刷新。' : '没有匹配的路由。可清空搜索或新增路由。'}</p>`;
    updateControls();
  }
  function serviceSuggestion(s) {
    if (s.service) return s.service;
    const address = s.addr;
    if (address && !['any','*','0.0.0.0','::'].includes(address)) {
      return `http://${address.includes(':') ? '[' + address + ']' : address}:${s.port}`;
    }
    return `http://127.0.0.1:${s.port}`;
  }
  function renderServices() {
    if (!S.services) return;
    const q = $('#services-filter').value.toLowerCase(), scope = $('#services-scope').value;
    const rows = S.services.filter(s => {
      const mapped = s.mapped || [];
      return (scope === 'all' || (scope === 'mapped' ? mapped.length : !mapped.length)) && [s.port,s.friendly,s.process,s.cmd,...mapped].join(' ').toLowerCase().includes(q);
    });
    $('#listener-list').innerHTML = rows.length ? rows.map(s => `<article class="list-row"><div><h2>${esc(s.friendly || s.process || '监听服务')} · ${esc(s.port)}</h2><p>${esc((s.addrs || [s.addr]).filter(Boolean).join(' · '))}</p><p class="muted wrap">${esc(s.cmd || s.process || '进程未提供')}</p><p>映射：${esc((s.mapped || []).join(' · ') || '未映射')}</p></div><div class="actions">${status(s.probe?.ok === true ? '探测通过' : s.probe?.ok === false ? '探测未通过' : '未探测', s.probe?.ok === true ? 'good' : s.probe?.ok === false ? 'bad' : 'pending')}<button type="button" data-bind="${esc(s.port)}" data-service="${esc(serviceSuggestion(s))}">映射域名</button></div>${s.probe?.detail ? `<p class="muted wrap">${esc(s.probe.detail)}</p>` : ''}</article>`).join('') : '<p class="empty">没有匹配的监听服务。</p>';
    updateControls();
  }
  function renderCdn() {
    if (!S.cdn) return;
    const c = S.cdn, uncertain = S.parts.cdn?.error || c.verified !== true;
    $('#cdn-summary').innerHTML = `<dl><dt>入口 CNAME</dt><dd>${esc(c.entry || '未配置')}</dd><dt>当前目标</dt><dd>${esc(c.target || '未知')} ${status(uncertain ? '未验证 / 当前状态未确认' : '已验证', uncertain ? 'pending' : 'good')}</dd><dt>改善阈值</dt><dd>${c.min_improvement == null ? '后端未提供' : `${esc(c.min_improvement)}%（可用性恢复另行判定）`}</dd></dl>`;
    const rows = S.ranked || c.candidates;
    $('#cdn-candidates').innerHTML = rows.length ? rows.map(r => {
      const domain = typeof r === 'string' ? r : r.domain;
      const latency = Number.isFinite(r.score) ? `${r.score.toFixed(1)} ms` : '未测速';
      const eligibility = r.eligible === true || r.usable === true ? 'HTTPS 样本可用' : r.eligible === false || r.usable === false ? '不符合 HTTPS 可用条件' : 'HTTPS 未验证';
      const business = r.business_verified === true || r.verified === true ? '业务验证通过' : '业务验证未确认';
      return `<article class="list-row"><div><h3>${esc(domain)}</h3><p>${esc(latency)} · ${esc(eligibility)} · ${esc(business)}</p>${r.success_rate != null ? `<p class="muted">HTTPS 成功率 ${Number.isFinite(r.success_rate) ? Math.round(r.success_rate * 100) + '%' : '未知'}</p>` : ''}</div><button type="button" data-cdn-set="${esc(domain)}" data-current="${domain === c.target}">${domain === c.target ? '当前目标' : '切换…'}</button></article>`;
    }).join('') : '<p class="empty">未配置候选线路。请在所选配置环境中配置候选后重试。</p>';
    updateControls();
  }
  /* 只展示配置/依赖结构与凭据的 configured/source，永不展开凭据对象。 */
  function safeRows(value, prefix = '') {
    if (!value || typeof value !== 'object') return `<p>${esc(value ?? '未提供')}</p>`;
    return `<dl>${Object.entries(value).filter(([key]) => !/(token|secret|password|salt|private|credential|api.?key)/i.test(key)).map(([key,v]) => {
      const label = prefix ? `${prefix}.${key}` : key;
      return typeof v === 'object' && v !== null ? `<dt>${esc(label)}</dt><dd>${safeRows(v)}</dd>` : `<dt>${esc(label)}</dt><dd>${esc(v === true ? '是' : v === false ? '否' : v ?? '未提供')}</dd>`;
    }).join('')}</dl>`;
  }
  function renderEnvironment() {
    const e = S.environment;
    $('#environment-content').innerHTML = `${status(e.ready ? '环境已就绪' : '环境未就绪', e.ready ? 'good' : 'bad')}
      <h2>缺失项</h2>${Array.isArray(e.missing) && !e.missing.length ? '<p>未报告缺失项</p>' : safeRows(e.missing || '后端未提供')}
      <h2>配置与来源</h2>${safeRows(e.config)}<h2>运行依赖</h2>${safeRows(e.dependencies)}
      <h2>凭据</h2><dl><dt>配置状态</dt><dd>${e.credentials?.configured === true ? '已配置（不代表 API 权限已验证）' : e.credentials?.configured === false ? '未配置' : '未提供'}</dd><dt>来源</dt><dd>${esc(e.credentials?.source || '未提供')}</dd></dl>${e.recovery ? `<h2>恢复建议</h2>${safeRows(e.recovery)}` : ''}`;
  }
  function renderAudit(rows) {
    $('#journal-list').innerHTML = rows.length ? [...rows].reverse().map(r => `<article class="list-row"><div><b>${esc(r.event || '事件未提供')}</b><p>${esc([r.op,r.hostname,r.error].filter(Boolean).join(' · ') || '—')}</p><p class="muted">${esc(timeText(r.ts))} · 配置：${esc(profileName(r.profile || r.args?.profile))} · ${esc(r.actor || r.ip || '操作者未提供')}</p></div></article>`).join('') : '<p class="empty">尚无审计记录。</p>';
  }

  /* 原生 dialog + 单一 form submit：Enter 不产生双重提交，Tab/Esc 由浏览器约束。 */
  let modalAction = null, modalReturn = null, modalEpoch = 0, modalVersion = 0;
  function field(label, id, value = '', placeholder = '', type = 'text') {
    return `<div class="field"><label for="${id}">${esc(label)}</label><input id="${id}" type="${type}" value="${esc(value)}" placeholder="${esc(placeholder)}" autocomplete="off" aria-describedby="${id}-error"><p id="${id}-error" class="field-error" role="alert"></p></div>`;
  }
  function openModal(title, body, onSubmit, {label = '确认', danger = false} = {}) {
    if ($('#modal').open) $('#modal').close();
    modalReturn = document.activeElement; modalEpoch = S.epoch; modalVersion++;
    modalAction = onSubmit;
    $('#modal-title').textContent = title; $('#modal-profile').textContent = `配置：${profileName(S.profile)}`;
    $('#modal-body').innerHTML = body; $('#modal-error').textContent = '';
    $('#modal-ok').textContent = label; $('#modal-ok').className = danger ? 'danger' : 'primary';
    $('#modal-ok').disabled = false;
    $('#modal').showModal();
    ($('#modal-body input') || $('#modal-cancel')).focus();
  }
  function fieldError(id, text) {
    const input = $(`#${id}`), error = $(`#${id}-error`);
    if (error) error.textContent = text;
    if (input) { input.setAttribute('aria-invalid','true'); input.focus(); }
    return false;
  }
  function validHostname(raw) {
    const host = raw.trim().toLowerCase().replace(/\.$/, '');
    return host.length <= 253 && host.split('.').length > 1 && host.split('.').every(p => /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(p)) ? host : null;
  }
  function serviceValue(raw) {
    let v = raw.trim(); if (/^:?\d+$/.test(v)) v = `http://127.0.0.1:${v.replace(/^:/,'')}`;
    if (!v.includes('://')) v = `http://${v}`;
    const match = /^(https?):\/\/([a-zA-Z0-9.-]+|\[[a-fA-F0-9:]+\])(?::(\d{1,5}))?(\/[^\s]*)?$/.exec(v);
    return match && (!match[3] || +match[3] >= 1 && +match[3] <= 65535) ? v : null;
  }
  function externalInfo(route) {
    openModal('外部域名：手动 DNS', `<p><code>${esc(route.hostname)}</code> 的 DNS 不由当前账户管理。此操作不提交后台任务。</p><p>在终端加载与此配置相同的环境后执行 <code>cf-tunnel-accel env</code> 核对来源。新增使用：</p><pre>cf-tunnel-accel add ${esc(route.hostname)} http://127.0.0.1:8080 --external</pre><p>按 CLI 提示手动添加 DCV 和 DNS-only CNAME，返回终端确认。切换/删除时在域名 DNS 平台手动处理相应记录；CLI 的 <code>switch</code> / <code>del</code> 仅处理其可管理部分。</p>`, () => true, {label: '知道了'});
  }
  function openRouteForm(route = null, preset = '') {
    if (!writeReady() || busy()) return;
    const zones = (S.state.zones || []).filter(z => z.status === 'active').map(z=>z.name);
    openModal(route ? '修改路由后端' : '新增路由', `${route ? `<p>域名：<b>${esc(route.hostname)}</b>。只修改 ingress 后端，可能重启隧道，不更改 DNS/SaaS。</p>` : field('业务域名','f-host','','app.example.com')}
      ${field('本机后端','f-service',route?.service || preset,'http://127.0.0.1:8080')}
      <label class="checkbox" for="f-noverify"><input id="f-noverify" type="checkbox" ${route?.no_tls_verify ? 'checked' : ''}>跳过源站 TLS 证书校验（noTLSVerify）</label>
      <p class="muted">HTTPS 默认校验证书；自签源站可显式选择跳过。证书校验开启并不等同于链路失败。</p>
      ${route ? '' : `<p>当前可管理 zone：${esc(zones.join(' · ') || '未确认')}。</p><p>依次配置 ingress / SaaS / DCV / CNAME 并验收。失败可能留下部分修改，请核对实际状态；外部域名请走路由页的 CLI 手动 DNS 流程。</p>`}`, async () => {
        const host = route?.hostname || validHostname($('#f-host').value);
        if (!host) return fieldError('f-host','请输入有效域名');
        const service = serviceValue($('#f-service').value);
        if (!service) return fieldError('f-service','请输入 http(s)://主机[:端口][/路径]；指定端口须为 1–65535');
        if (!route && !zones.some(z => host.endsWith(`.${z}`) || host === z)) return fieldError('f-host','域名不属于已确认的 active zone。外部 DNS 需在 CLI 交互配置，不提交 UI 任务。');
        await submitJob({op: route ? 'update' : 'add', hostname: host, service, no_tls_verify: $('#f-noverify').checked});
        return true;
      }, {label: route ? '保存后端' : '开始配置'});
  }
  function routeAction(action, host) {
    const route = S.state?.routes.find(r => r.hostname === host);
    if (!route) return;
    if (action === 'edit') return openRouteForm(route);
    if (action === 'health') return startHealth(host, true);
    if (route.external === true) return externalInfo(route);
    if (route.external == null) { message('域名 zone 归属未确认，请刷新状态；不提交 DNS 变更。'); return; }
    if (action === 'delete') {
      openModal('删除路由', `<p>将删除 <b>${esc(host)}</b> 的 Tunnel ingress、SaaS 自定义主机名和当前账户 zone 内的 DNS 记录。此操作不可直接撤销，失败后可能存在部分修改。</p>${field('输入完整域名确认删除','f-confirm','',''+host)}`, async () => {
        if ($('#f-confirm').value.trim() !== host) return fieldError('f-confirm','请原样输入完整域名');
        await submitJob({op:'delete',hostname:host,confirm:host}); return true;
      }, {label:'确认删除',danger:true});
    } else {
      openModal('切到优选入口', `<p>将 <b>${esc(host)}</b> 的 CNAME 指向 <code>${esc(S.state.preferred.entry || '未配置')}</code>。可能补 SaaS 验证记录并等待证书签发。</p><p>优选目标：${esc(S.state.preferred.target || '未知')}；${S.state.preferred.verified === true ? '已验证' : '尚未验证，不视为已正常生效'}。</p>`, async () => { await submitJob({op:'switch',hostname:host}); return true; }, {label:'确认切换'});
    }
  }
  function confirmCdn(target, best = false) {
    if (!writeReady() || busy() || !S.cdn || (S.parts.cdn?.error && !S.parts.cdn?.partial)) return;
    const c = S.cdn;
    openModal(best ? '测速并自动优选' : '切换优选 CNAME', `<p>入口：<code>${esc(c.entry || '未配置')}</code>；当前目标：<code>${esc(c.target || '未知')}</code>（${c.verified === true ? '已验证' : '未验证'}）。影响此入口承载的全部业务域名。</p>${best ? `<p>测量候选并按可用性及改善阈值 ${esc(c.min_improvement ?? '后端配置')}% 判断。自动切换需要业务 Host/SNI 验证；全部失败时不得改 DNS。未达到阈值可能保持原目标。</p>` : `<p>将 CNAME 改为 <b>${esc(target)}</b>，这不是纯测速。DNS 缓存可能延迟生效；读取任务日志确认实际结果。</p>`}<p>多阶段修改不是跨服务事务；API 或链路确认失败时应检查实际记录与回滚结果。</p>`, async () => {
      await submitJob(best ? {op:'cdn-best'} : {op:'cdn-set',target,confirm:target}); return true;
    }, {label: best ? '确认测速并优选' : '确认修改 CNAME'});
  }
  async function openDetail(host) {
    openModal('路由详情', '<p id="host-detail">正在读取…</p>', () => true, {label:'关闭'});
    const node = $('#host-detail'), version = modalVersion;
    try {
      const {host: h} = await api(`/api/host/${encodeURIComponent(host)}?https=1`);
      if (version !== modalVersion || !$('#modal').open) return;
      node.innerHTML = `<h3>${esc(host)}</h3>${errorsText(h.errors) ? `<p class="notice error">部分检查未完成：${esc(errorsText(h.errors))}</p>` : ''}<dl><dt>后端</dt><dd>${esc(h.service || '未提供')}</dd><dt>DNS</dt><dd>${esc(h.dns?.cname || h.dns?.a || '未确认')}</dd><dt>SaaS / SSL</dt><dd>${esc(h.saas ? `${h.saas.status || '未知'} / ${h.saas.ssl || '未知'}` : '未确认')}</dd><dt>本机探测</dt><dd>${h.probe?.ok === true ? '通过' : h.probe?.ok === false ? '未通过' : '未探测'}</dd><dt>HTTPS</dt><dd>${h.https?.ok === true ? '通过' : h.https?.ok === false ? '未通过' : '未验证'}</dd></dl><pre>${esc([h.probe?.detail,h.https?.detail,...(h.saas?.errors || [])].filter(Boolean).join('\n'))}</pre>`;
    } catch (err) { if (!quiet(err) && version === modalVersion) node.textContent = `读取失败：${err.message}。关闭后可重试。`; }
  }

  /* 任务：游标 next 总是前进，offset 表示后端保留窗口。断线不宣告任务失败。 */
  function jobStatus(job, lost = false) {
    if (lost && isPending(job)) return '状态待同步（上次：' + (job.status === 'running' ? '执行中' : '排队中') + '）';
    if (job.status === 'interrupted') return `执行已中断，结果未确认；不会自动重试${job.error ? '：' + job.error : ''}`;
    if (job.status === 'error' || job.error || job.op !== 'health' && job.result?.ok === false) return `任务失败${job.error || job.result?.error ? '：' + (job.error || job.result.error) : ''}`;
    if (job.status === 'done') {
      if (job.op === 'health') return job.result?.ok === true ? '执行结束，检查通过' : job.result?.ok === false ? '执行结束，检查未通过' : '执行结束，检查结果未提供';
      return '执行结束' + (['warning','failed'].includes(job.outcome) ? '，请核对结果与日志' : '') + (job.result?.changed === false ? '，未发生变更' : '');
    }
    return job.status === 'queued' ? '排队中' : job.status === 'running' ? '执行中（无百分比估算）' : '状态未知';
  }
  function ingestJob(job) {
    if (!job?.id || typeof job.id !== 'string') throw new Error('任务快照格式错误');
    const previous = S.jobs.get(job.id);
    // Terminal snapshots are immutable: an older in-flight list must not reopen a finished task.
    if (previous && !isPending(previous) && isPending(job)) return;
    S.jobs.set(job.id, {...previous,...job});
    if ((!previous || isPending(previous)) && !isPending(job)) {
      if (job.id === S.selectedJob) S.detailDirty = true;
      if (job.profile === S.profile) {
        if (job.result?.ranked) S.ranked = job.result.ranked;
        refreshScoped();
        if (job.op === 'health') $('#diag-result').textContent = jobStatus(job);
      }
      loadPart('audit');
      message(`${profileName(job.profile)} · ${job.op}：${jobStatus(job)}`);
    }
  }
  function acceptJobList(data) {
    if (!Array.isArray(data.jobs)) throw new Error('任务列表格式错误');
    data.jobs.forEach(ingestJob);
    S.jobsSupported = true; S.jobsLost = false; S.jobsLoaded = true;
    if (S.uncertain) {
      S.uncertain = false;
      message('已同步任务列表。请核对任务归属与操作，再决定是否手动提交；未重放写请求。');
    }
    const active = data.active || data.jobs.find(isPending)?.id;
    const saved = stored(sessionStorage, JOB_KEY);
    if (!S.selectedJob && (active || saved || data.jobs[0]?.id)) chooseJob(active || saved || data.jobs[0].id);
    renderJobs();
  }
  function renderJobs() {
    const jobs = [...S.jobs.values()].sort((a,b) => (b.created || 0) - (a.created || 0));
    $('#job-list').innerHTML = jobs.length ? jobs.map(j => `<article class="list-row"><div><b>${esc(j.op)} · ${esc(profileName(j.profile))}</b><p>${esc(jobStatus(j, j.id === S.selectedJob ? S.detailLost : S.jobsLost))}</p><p class="muted">${esc(j.id)} · 创建 ${esc(timeText(j.created))}${j.ended ? ' · 结束 ' + esc(timeText(j.ended)) : ''}</p></div><button type="button" data-job="${esc(j.id)}" ${j.id === S.selectedJob ? 'aria-current="true"' : ''}>${j.id === S.selectedJob ? '当前详情' : '查看日志'}</button></article>`).join('') : `<p class="empty">${S.jobsLoaded ? '尚无任务记录。' : '任务列表尚未同步。'}</p>`;
    updateControls();
  }
  function renderBanner() {
    const job = [...S.jobs.values()].find(isPending) || S.jobs.get(S.selectedJob);
    $('#task-banner').hidden = !job && !S.uncertain;
    $('#task-title').textContent = S.uncertain ? '写请求回执未确认' : job ? `${job.op} · 配置 ${profileName(job.profile)}` : '';
    $('#task-status').textContent = S.uncertain ? '状态待同步。请求可能已执行；先刷新任务列表核对，不自动重试写入。' : job ? jobStatus(job, job.id === S.selectedJob ? S.detailLost : S.jobsLost) : '';
    $('#task-reconnect').hidden = !(S.uncertain || S.jobsLost || S.detailLost);
  }
  async function submitJob(payload) {
    if (!S.authenticated || !S.profile) throw new Error('请登录并选择配置');
    if (busy()) throw new Error('已有任务或未确认的提交，请先同步任务状态');
    if (!['health','cdn-test'].includes(payload.op) && !writeReady()) throw new Error('配置或状态未就绪，请先刷新路由并检查环境');
    S.submitting = true; updateControls();
    try {
      const data = await api('/api/jobs', {method:'POST',body:payload});
      if (!data.job?.id) throw Object.assign(new Error('任务提交响应缺少 ID，请先同步任务列表'), {ambiguous:true});
      ingestJob(data.job); S.jobsLost = false;
      chooseJob(data.job.id); renderJobs(); loadPart('audit');
      message('任务已提交。请查看任务状态与实际结果。');
    } catch (err) {
      if (err.ambiguous || quiet(err)) {
        S.uncertain = true;
        throw new Error('提交回执未确认，任务可能已接收。先同步任务列表，勿重复提交。表单已保留。');
      }
      throw err;
    } finally { S.submitting = false; updateControls(); }
  }
  function chooseJob(id) {
    S.selectedJob = id; S.cursor = 0; S.logs = []; S.omitted = 0; S.detailLost = false; S.detailGeneration++;
    store(sessionStorage, JOB_KEY, id);
    $('#job-log').textContent = '正在读取日志…'; $('#job-result').replaceChildren(); $('#log-omitted').hidden = true;
    $('#job-detail-meta').textContent = `任务 ${id}`;
    pollDetail(); renderJobs();
  }
  async function pollDetail() {
    if (!S.selectedJob || !S.authenticated) return;
    const id = S.selectedJob, generation = S.detailGeneration, cursor = S.cursor;
    // Prevent simultaneous polls for the same cursor, but allow choosing another job immediately.
    if (S.pollKey === `${generation}:${cursor}`) return;
    const key = `${generation}:${cursor}`; S.pollKey = key;
    try {
      const {job, store_error: storeError} = await api(`/api/jobs/${encodeURIComponent(id)}?since=${cursor}`);
      if (generation !== S.detailGeneration) return;
      if (!job || !Array.isArray(job.lines) || !Number.isSafeInteger(job.next) || job.next < cursor) throw new Error('任务游标异常，请重新同步任务详情');
      const known = S.jobs.get(id);
      if (known && !isPending(known) && isPending(job)) { S.detailDirty = true; return; }
      const offset = Number.isSafeInteger(job.offset) ? job.offset : 0;
      if (offset > cursor) S.omitted += offset - cursor;
      S.logs.push(...job.lines.map(l => `${Number.isFinite(l.t) ? l.t.toFixed(1) + 's' : '—'}  ${l.m ?? ''}`));
      if (S.logs.length > 2000) { S.omitted += S.logs.length - 2000; S.logs.splice(0,S.logs.length - 2000); }
      // Advance even when no lines arrived; backend cursor is global, not retained array length.
      S.cursor = job.next; S.detailLost = false; ingestJob(job); S.detailDirty = false;
      const log = $('#job-log'), atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
      log.textContent = S.logs.join('\n') || '尚无日志'; if (atBottom) log.scrollTop = log.scrollHeight;
      $('#log-omitted').hidden = !S.omitted; $('#log-omitted').textContent = `已省略 ${S.omitted} 行旧日志（后端保留窗口 / 前端日志上限）。`;
      $('#job-detail-meta').textContent = `${job.id} · ${job.op} · 配置 ${profileName(job.profile)} · 创建 ${timeText(job.created)} · ${Number.isFinite(job.elapsed) ? job.elapsed.toFixed(1) + 's' : '耗时未提供'}${job.ended ? ' · 结束 ' + timeText(job.ended) : ''}`;
      $('#job-detail-notice').textContent = jobStatus(job) + (storeError ? `；任务记录存储异常：${storeError}，刷新后恢复可能不完整` : '');
      $('#job-detail-notice').className = `notice ${job.status === 'error' || job.result?.ok === false ? 'error' : storeError || job.status === 'interrupted' ? 'warning' : ''}`;
      $('#job-result').innerHTML = job.result ? `<h3>执行结果</h3>${safeRows(job.result)}` : '';
      if (job.op === 'health' && job.profile === S.profile) $('#diag-result').textContent = jobStatus(job);
      renderJobs();
    } catch (err) {
      if (quiet(err) || generation !== S.detailGeneration) return;
      S.detailLost = true;
      $('#job-detail-notice').textContent = `状态待同步：${err.message}。读取会自动重连，写请求不会重放。`;
      $('#job-detail-notice').className = 'notice warning'; renderBanner();
    } finally { if (S.pollKey === key) S.pollKey = null; }
  }
  async function syncTasks() { await Promise.allSettled([loadPart('jobs'), pollDetail(), loadPart('audit')]); }
  async function tick() {
    if (!S.authenticated || S.tickBusy) return;
    S.tickBusy = true;
    try {
      const pending = [...S.jobs.values()].some(isPending);
      if (pending || S.detailLost || S.uncertain || !S.lastJobList || Date.now() - S.lastJobList > 15000) {
        S.lastJobList = Date.now(); await loadPart('jobs');
      }
      if (S.selectedJob && (isPending(S.jobs.get(S.selectedJob)) || S.detailLost || S.detailDirty)) await pollDetail();
      // Observe pending tasks even if the user chose an older completed job.
      for (const j of [...S.jobs.values()].filter(j => isPending(j) && j.id !== S.selectedJob)) {
        try { const data = await api(`/api/jobs/${encodeURIComponent(j.id)}?since=${j.next || 0}`); ingestJob(data.job); renderJobs(); }
        catch (err) { if (!quiet(err)) { S.jobsLost = true; renderBanner(); } }
      }
    } finally { S.tickBusy = false; }
  }
  async function startHealth(host = '', https = true) {
    $('#diag-error').textContent = '';
    try {
      await submitJob({op:'health', ...(host ? {hostname:host} : {}), https});
      $('#diag-result').textContent = '任务已提交，正在同步检查结果；详情见任务审计。';
    } catch (err) { $('#diag-error').textContent = err.message; if ($('#diagnostics').hidden) message(`检查未提交：${err.message}`); }
  }

  function navigate() {
    const ids = ['routes','services','cdn','diagnostics','activity','environment'];
    const id = ids.includes(location.hash.slice(1)) ? location.hash.slice(1) : 'routes';
    $$('.page').forEach(p => { p.hidden = p.id !== id; });
    $$('.nav a').forEach(a => { if (a.hash === `#${id}`) a.setAttribute('aria-current','page'); else a.removeAttribute('aria-current'); });
    document.title = `${$('.nav [aria-current]').textContent} · 隧道控制台`;
  }
  async function boot() {
    // A completed session probe may race a user's login; invalidate its stale 401.
    if (!S.authenticated) S.authEpoch++;
    S.authenticated = true; S.csrf = S.csrf || cookie();
    $('#gate').close(); $('#app').inert = false; $('#profile-select').focus();
    const saved = stored(sessionStorage,JOB_KEY);
    if (saved && !S.selectedJob) chooseJob(saved);
    await loadProfiles();
    await Promise.allSettled([loadPart('jobs'),loadPart('audit')]);
  }
  async function checkSession() {
    $('#gate-retry').disabled = true;
    try {
      const data = await api('/api/session');
      if (data.authenticated) await boot(); else openGate('请输入访问凭据');
    } catch (err) { if (!quiet(err)) openGate(`会话检查失败：${err.message}；可重试检查或登录。`); }
    finally { $('#gate-retry').disabled = false; }
  }
  // Native modal makes the background inert. Explicit cycling also avoids a transient
  // focus on document.body in Chromium when tabbing past the final control.
  ['gate','modal'].forEach(id => $(`#${id}`).addEventListener('keydown', e => {
    if (e.key !== 'Tab') return;
    const dialog = e.currentTarget;
    const controls = $$('button:not(:disabled), input:not(:disabled), select:not(:disabled), a[href], [tabindex="0"]', dialog)
      .filter(n => !n.hidden && n.getClientRects().length);
    const first = controls[0], last = controls[controls.length - 1];
    if (!first) { e.preventDefault(); dialog.focus(); }
    else if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }));
  $('#gate').addEventListener('cancel', e => e.preventDefault());
  $('#gate-form').addEventListener('submit', async e => {
    e.preventDefault();
    if ($('#gate-submit').disabled) return;
    const token = $('#token').value.trim(); $('#token').value = '';
    if (!token) { $('#gate-msg').textContent = '请输入访问凭据'; return; }
    $('#gate-submit').disabled = true; $('#gate-msg').textContent = '正在验证…';
    try { const data = await api('/api/session',{method:'POST',body:{token}}); S.csrf = data.csrf || cookie(); await boot(); }
    catch (err) { if (!quiet(err)) { $('#gate-msg').textContent = err.message; $('#token').focus(); } }
    finally { $('#token').value = ''; $('#gate-submit').disabled = false; }
  });
  $('#gate-retry').addEventListener('click',checkSession);
  $('#logout').addEventListener('click', async () => {
    $('#logout').disabled = true;
    try {
      await api('/api/session',{method:'DELETE'}); S.csrf = ''; S.selectedJob = null; S.jobs.clear();
      store(sessionStorage,JOB_KEY,null); openGate('已退出登录');
    } catch (err) { if (!quiet(err)) message(`退出未确认：${err.message}；请重试退出。`); }
    finally { $('#logout').disabled = false; }
  });
  $('#modal-form').addEventListener('submit', async e => {
    e.preventDefault();
    if ($('#modal-ok').disabled || modalEpoch !== S.epoch) return;
    const version = modalVersion;
    $('#modal-error').textContent = '';
    $$('.field-error', $('#modal-body')).forEach(n=> { n.textContent = ''; });
    $$('[aria-invalid]', $('#modal-body')).forEach(n=>n.removeAttribute('aria-invalid'));
    $('#modal-ok').disabled = true;
    try { if (await modalAction?.() !== false && version === modalVersion) $('#modal').close(); }
    catch (err) { if (version === modalVersion) $('#modal-error').textContent = err.message; }
    finally { if (version === modalVersion) $('#modal-ok').disabled = false; }
  });
  $('#modal-cancel').addEventListener('click',()=>$('#modal').close());
  $('#modal').addEventListener('close', () => {
    modalVersion++;
    if (!$('#gate').open && modalReturn?.isConnected && !modalReturn.disabled) modalReturn.focus();
    else if (!$('#gate').open) $('#main').focus();
  });
  $('#profile-select').addEventListener('change', e=>selectProfile(e.target.value));
  $('#profile-retry').addEventListener('click',loadProfiles);
  $('#routes-refresh').addEventListener('click',()=>loadPart('state',{fresh:true}));
  $('#services-refresh').addEventListener('click',()=>loadPart('services',{probe:true}));
  $('#cdn-refresh').addEventListener('click',()=>loadPart('cdn'));
  $('#environment-refresh').addEventListener('click',()=>loadPart('environment'));
  $('#activity-refresh').addEventListener('click',syncTasks);
  $('#task-reconnect').addEventListener('click',syncTasks);
  $('#task-open').addEventListener('click',()=> {
    const pending = [...S.jobs.values()].find(isPending);
    if (pending && pending.id !== S.selectedJob) chooseJob(pending.id);
  });
  $('#route-add').addEventListener('click',()=>openRouteForm());
  $('#routes-filter').addEventListener('input',renderRoutes);
  $('#services-filter').addEventListener('input',renderServices);
  $('#services-scope').addEventListener('change',renderServices);
  $('#route-grid').addEventListener('click',e=> {
    const b = e.target.closest('button'); if (!b || b.disabled) return;
    if (b.dataset.detail) openDetail(b.dataset.detail);
    else if (b.dataset.routeAction) routeAction(b.dataset.routeAction,b.closest('[data-host]').dataset.host);
  });
  $('#listener-list').addEventListener('click', e=> { const b = e.target.closest('[data-bind]'); if (b && !b.disabled) openRouteForm(null,b.dataset.service); });
  $('#cdn-candidates').addEventListener('click', e=> { const b = e.target.closest('[data-cdn-set]'); if (b && !b.disabled) confirmCdn(b.dataset.cdnSet); });
  $('#cdn-test').addEventListener('click', async ()=> { try { await submitJob({op:'cdn-test'}); } catch (err) { message(err.message); } });
  $('#cdn-best').addEventListener('click',()=>confirmCdn(null,true));
  $('#cdn-manual-form').addEventListener('submit',e=> {
    e.preventDefault(); if ($('#cdn-manual-go').disabled) return;
    $('#cdn-manual-error').textContent = '';
    const target = validHostname($('#cdn-manual').value);
    if (!target) { $('#cdn-manual-error').textContent = '请输入有效目标域名'; $('#cdn-manual').focus(); return; }
    confirmCdn(target);
  });
  $('#health-form').addEventListener('submit',e=> {
    e.preventDefault(); if ($('#diag-run').disabled) return;
    const raw = $('#diag-host').value.trim(), host = raw ? validHostname(raw) : '';
    if (raw && !host) { $('#diag-error').textContent = '请输入有效域名或留空检查全部'; $('#diag-host').focus(); return; }
    startHealth(host,$('#diag-https').checked);
  });
  $('#job-list').addEventListener('click',e=> { const b = e.target.closest('[data-job]'); if (b) chooseJob(b.dataset.job); });
  window.addEventListener('hashchange',navigate);
  window.addEventListener('online',()=> { S.offline = false; if (S.authenticated) { syncTasks(); refreshScoped(); } });
  window.addEventListener('offline',()=> { S.offline = true; S.jobsLost = true; S.detailLost = !!S.selectedJob; renderBanner(); updateMeta(); });
  navigate(); openGate('正在检查会话…'); checkSession();
  setInterval(tick,2000); setInterval(updateMeta,10000);
})();

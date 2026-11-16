'use strict';

const demo = window.MISTAKE_DEMO;
const icons = window.DEMO_ICONS;
const catalog = window.MistakePrototypeCatalog;
const review = window.MistakePrototypeReview;
const view = document.getElementById('view');
const dialog = document.getElementById('modal');
const modal = document.getElementById('modal-body');
const storageKey = 'hsc-mistake-book-prototype-v1-samples';
const demoDate = '2026-11-09';
const blankFilters = () => ({ query: '', date: 'all', from: '', to: '', subject: '', cause: '', topic: '' });
const state = {
  page: 'library', selected: demo.cases[0].id, tab: 'redo', filters: blankFilters(), grade: '六年级',
  topic: demo.topics[0].id, topicStep: -1, coachMode: 'socratic', coachStep: 0,
  drafts: {}, modal: null, captureIndex: 0, captureStep: 1, captureSource: 'manual',
  searchOpen: false, suggestionIndex: -1, reviewMethod: '', reviewDraft: '',
};
let saved = { edits: {}, imported: [], attempts: {}, reviews: {}, labels: {}, pending: {}, sessionNotes: {}, paused: {} };
try {
  const stored = JSON.parse(localStorage.getItem(storageKey) || 'null');
  if (stored && typeof stored.edits === 'object' && Array.isArray(stored.imported) && typeof stored.attempts === 'object') saved = { ...saved, ...stored };
} catch { /* A fresh local preview can run without browser storage. */ }
if (['六年级', '八年级'].includes(saved.activeGrade)) state.grade = saved.activeGrade;
let toastTimer;
let restoreFocus = null;

const html = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const icon = name => `<span class="icon" aria-hidden="true">${icons[name] || ''}</span>`;
const tag = (text, tone = 'neutral', name = '') => `<span class="tag ${tone}">${name ? icon(name) : ''}${html(text)}</span>`;
const selectedOption = (value, label, current) => `<option value="${html(value)}" ${current === value ? 'selected' : ''}>${html(label)}</option>`;
const photo = (item, className = '') => `<img ${className ? `class="${className}"` : ''} src="${html(item.image)}" alt="${html(item.title)}的示例原题裁图" draggable="false">`;
const showDate = date => date.replaceAll('-', '.');
const shortDate = date => showDate(date).slice(5);
const unique = values => [...new Set(values)];
const titleByPage = { study: '当前学习', library: '错题库', review: '今日复习', topics: '高频题型' };
const neutralTitle = item => ({
  'math-fractions-1': '分数除法 · 第 12 题',
  'math-fractions-2': '分数除法 · 第 13 题',
  'physics-heat-2': '热水与冷水 · 热量流向',
})[item.id.replace(/^capture-/, '')] || item.title;

function allCases() {
  const extra = saved.imported.flatMap(record => {
    const sample = demo.cases.find(item => item.id === record.caseId);
    if (!sample || typeof record.question !== 'string') return [];
    return [{ ...sample, id: `capture-${sample.id}`, question: record.question, answer: record.answer, date: demoDate, stage: '待复习', due: true, ocrStatus: 'verified', captured: record.source || 'manual' }];
  });
  return [...extra, ...demo.cases.filter(item => !item.captureOnly)].map(item => {
    const edit = saved.edits[item.id];
    const corrected = edit && typeof edit.question === 'string' ? { ...item, question: edit.question, ocrStatus: 'verified', edited: true } : item;
    return Object.hasOwn(saved.labels, item.id) ? { ...corrected, tags: saved.labels[item.id] } : corrected;
  }).filter(item => catalog.recordGrade(item) === state.grade);
}

function currentItem() { return allCases().find(item => item.id === state.selected); }
function currentTopic() { return demo.topics.find(topic => topic.id === state.topic); }
function attemptKey(id, kind) { return `${kind}:${id}`; }
function reviewLog(item) { return saved.reviews[item.id] || { wrong: 0, latest: null }; }
function reviewed(item) {
  const { latest } = reviewLog(item);
  return latest ? latest.outcome === 'correct' : saved.attempts[attemptKey(item.id, 'case')]?.correct === true;
}
function dueForReview(item) { return item.due || reviewLog(item).latest?.outcome === 'incorrect'; }
function reviewStage(item) { return reviewed(item) ? '待复测' : reviewLog(item).wrong ? '未掌握' : item.stage; }
function resetAttempt() { state.reviewMethod = ''; state.reviewDraft = ''; state.reviewMessage = ''; }
function save() {
  try { localStorage.setItem(storageKey, JSON.stringify(saved)); return true; }
  catch { return false; }
}
function announce(message) {
  const toast = document.getElementById('toast');
  toast.textContent = message;
  toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.hidden = true; }, 3500);
}
function announceSaved(message) { announce(save() ? message : `${message}（当前页面有效，浏览器存储不可用）`); }

function dateMatches(date, f) {
  if (f.date === 'today') return date === demoDate;
  if (f.date === 'week') return date >= '2026-11-03' && date <= demoDate;
  if (f.date === 'previous') return date >= '2026-11-02' && date <= '2026-11-08';
  if (f.date === 'custom') return (!f.from || date >= f.from) && (!f.to || date <= f.to);
  return true;
}
function filterCases(cases, f, useTopic = true) {
  const period = f.query.match(/近\s*(?:7|七)\s*天|最近一周|最近|上周|今天/);
  const hintedDate = period?.[0] === '上周' ? 'previous' : period?.[0] === '今天' ? 'today' : 'week';
  const query = period ? f.query.replace(period[0], '').replace(/^(帮我)?(找|查|看)(一下)?\s*/, '').trim() : f.query;
  return cases.filter(item => dateMatches(item.date, f) && (!period || dateMatches(item.date, {date: hintedDate})) && (!f.subject || item.subject === f.subject)
    && (!f.cause || catalog.recordTags(item).includes(f.cause)) && (!useTopic || !f.topic || item.typeId === f.topic)
    && (!query || catalog.findMatches(item, query)));
}
function toolbar() {
  const f = state.filters;
  const cases = allCases();
  const select = (label, key, options) => `<label class="filter"><span>${label}</span><select aria-label="${label}" data-filter="${key}">${options.map(([value,text]) => selectedOption(value,text,f[key])).join('')}</select></label>`;
  const dates = [['all','全部日期'],['today','今天'],['week','近 7 天'],['previous','上周'],['custom','自选日期']];
  return `<div class="toolbar" aria-label="错题筛选">
    <form class="search-form" id="search-form" role="search"><div class="search-box">${icon('search')}<input type="text" id="query" role="combobox" aria-label="搜索错题" aria-autocomplete="list" aria-expanded="false" aria-controls="search-suggestions" placeholder="如：找最近做错的分数题" value="${html(f.query)}" maxlength="96" autocomplete="off"><button type="submit" class="search-submit" aria-label="查询">查找</button></div><div class="suggestions" id="search-suggestions" role="listbox" aria-label="搜索联想" hidden></div></form>
    ${select('日期','date',dates)}
    ${select('学科','subject',[['','全部学科'],...catalog.gradeSubjects(state.grade,cases).map(v => [v,v])])}
    ${select('错因','cause',[['','全部错因'],...unique(cases.flatMap(catalog.recordTags)).map(v => [v,v])])}
    ${f.topic && state.page !== 'topics' ? tag(demo.topics.find(topic => topic.id === f.topic)?.title || '已选题型','subject','tag') : ''}
    <button class="ghost" type="button" data-action="reset">${icon('rotate')}清空</button>
    ${f.date === 'custom' ? `<div class="custom-date"><label>起始日期 <input type="date" data-filter="from" value="${html(f.from)}"></label><span>至</span><label>结束日期 <input type="date" data-filter="to" value="${html(f.to)}"></label></div>` : ''}
  </div>`;
}

function searchChoices(query) {
  const scope = allCases().filter(item => state.page !== 'review' || dueForReview(item));
  const period = query.match(/近\s*(?:7|七)\s*天|最近一周|最近|上周|今天/);
  const rows = filterCases(scope, {...state.filters, query: period?.[0] || ''}, state.page !== 'topics');
  const partial = period ? query.replace(period[0], '').trim() : query;
  const picks = catalog.suggestions(rows, partial).slice(0, 5).map(pick => {
    const row = rows.find(item => item.id === pick.id);
    const selected = row ? {...pick, label:neutralTitle(row)} : pick;
    return period ? {...selected, query:`${period[0]} ${selected.query}`} : selected;
  });
  return query.trim() ? [...picks, {kind:'query',label:`查询「${query.trim()}」`,query:query.trim()}] : picks;
}
function updateSuggestions() {
  const input = document.getElementById('query');
  if (!input) return;
  const list = document.getElementById('search-suggestions');
  const picks = state.searchOpen ? searchChoices(input.value) : [];
  list.hidden = !picks.length;
  input.setAttribute('aria-expanded', String(!list.hidden));
  if (picks[state.suggestionIndex]) input.setAttribute('aria-activedescendant', `search-pick-${state.suggestionIndex}`);
  else input.removeAttribute('aria-activedescendant');
  list.innerHTML = picks.map((pick,i) => `<div id="search-pick-${i}" role="option" aria-selected="${state.suggestionIndex === i}" data-pick="${i}"><span>${html(pick.label)}</span><small>${({case:'题目',topic:'题型',cause:'错因',query:'完整查询'})[pick.kind] || '联想'}</small></div>`).join('');
}
function closeSuggestions() { state.searchOpen = false; state.suggestionIndex = -1; updateSuggestions(); }
function runSearch(index = -1) {
  const input = document.getElementById('query');
  const pick = searchChoices(input.value)[index];
  if (pick?.kind === 'case' && state.page === 'topics') go('library');
  state.filters.query = pick?.query ?? input.value.trim();
  document.getElementById('query').value = state.filters.query;
  if (pick?.kind === 'case') { state.selected = pick.id; state.tab = 'redo'; resetAttempt(); }
  closeSuggestions();
  renderResults();
}

function head(kicker, title, description, action = '') {
  return `<div class="page-head"><div><p class="eyebrow">${html(kicker)}</p><h1>${html(title)}</h1><p>${html(description)}</p></div>${action}</div>`;
}
function action(name, label, symbol, kind = 'secondary', attrs = '') {
  return `<button type="button" class="${kind}" data-action="${name}" ${attrs}>${symbol ? icon(symbol) : ''}${html(label)}</button>`;
}
function empty(title, description, reset = false) {
  return `<div class="empty">${icon('folder')}<strong>${html(title)}</strong><p>${html(description)}</p>${reset ? action('reset','清空筛选','rotate','secondary') : ''}</div>`;
}
function updateNavigation() {
  document.getElementById('crumb-current').textContent = titleByPage[state.page];
  document.getElementById('sample-grade').value = state.grade;
  document.getElementById('nav-due').textContent = allCases().filter(item => dueForReview(item) && !reviewed(item)).length;
  document.querySelectorAll('.navigation [data-nav], .session-link').forEach(button => {
    if (button.dataset.nav === state.page) button.setAttribute('aria-current','page');
    else button.removeAttribute('aria-current');
  });
}
function go(page, resetTab = true) {
  state.page = page;
  state.searchOpen = false; state.suggestionIndex = -1;
  if (resetTab) { state.tab = 'redo'; state.coachStep = 0; resetAttempt(); }
  render();
  window.scrollTo(0,0);
}
function observationBar() {
  const paused = saved.paused[state.grade];
  const pending = saved.pending[state.grade];
  return `<div class="observation-bar"><span class="device-status">${icon('scan')}<strong>高拍仪</strong><span class="status-dot ${paused ? 'paused' : ''}"></span>${paused ? '观察已暂停' : '持续观察中'}${tag('设备演示','neutral')}</span><div class="observation-actions">${action('observe',pending ? '核对 1 条候选' : '查看观察','scan','ghost')}${action('capture','手动录入','plus','ghost')}</div></div>`;
}
function studyView() {
  const due = allCases().filter(item => dueForReview(item) && !reviewed(item)).length;
  return `${head('学习工作区 · 会话演示','当前学习','同一个学习会话里，小知可以帮你整理错题、带你复习，再回到当前任务。')}${observationBar()}
    <div class="study-grid"><section class="panel study-card"><div class="section-title"><h2>今天的学习</h2>${tag('进行中','subject')}</div><p>${state.grade === '六年级' ? '数学 · 分数运算' : '物理 · 热现象'}</p><label class="modal-field">学习便签 <textarea id="study-notes" rows="3" maxlength="500" placeholder="在这里写一句，打开错题本再回来试试。">${html(saved.sessionNotes[state.grade] || '')}</textarea></label><small id="note-status" class="flow-note" role="status">便签保存在当前示例会话</small></section>
    <section class="panel study-card"><div class="section-title"><h2>小知的复习提醒</h2>${icon('spark')}</div><p>今天有 ${due} 道错题待复习。你可以说「小知，开始复习错题」。</p><div class="detail-actions">${action('open-book','打开错题本','book','secondary')}${action('open-review','今日复习','calendar','primary')}</div></section></div>`;
}
function render() {
  updateNavigation();
  state.searchOpen = false; state.suggestionIndex = -1;
  if (state.page === 'study') { view.innerHTML = studyView(); return; }
  if (state.page === 'topics') {
    view.innerHTML = `${head('跨题复习','高频题型','把同类错题放到一起，理解反复卡住的原因。')}${toolbar()}<div id="results"></div>`;
  } else {
    const isReview = state.page === 'review';
    const remaining = allCases().filter(item => dueForReview(item) && !reviewed(item)).length;
    view.innerHTML = `${head('错题研习', isReview ? '今日复习' : '智能错题本', isReview ? '先独立重做，再根据结果讲解和安排复测。' : '查找错题，先做一遍，再看讲解。', action('observe','高拍仪录入','scan','primary'))}${observationBar()}
      ${isReview ? `<div class="review-banner">${icon('calendar')}<p><strong>今天还有 ${remaining} 道待练</strong>。独立做对后，题目会进入待复测。</p></div>` : ''}
      ${toolbar()}<div id="results"></div>`;
  }
  renderResults();
}
function renderResults() {
  const target = document.getElementById('results');
  if (state.page === 'topics') { target.innerHTML = topicsView(); return; }
  const cases = allCases();
  const base = state.page === 'review' ? cases.filter(dueForReview) : cases;
  const items = filterCases(base, state.filters);
  if (!items.some(item => item.id === state.selected)) {
    state.selected = items[0]?.id || null;
    state.tab = 'redo'; resetAttempt();
  }
  const count = `${items.length} 道`;
  const item = items.find(row => row.id === state.selected);
  target.innerHTML = `<div class="workspace-grid">
    <section class="panel" aria-label="错题列表"><div class="list-head"><h2>${state.page === 'review' ? '到期错题' : '错题列表'}</h2><small role="status">${count} · 最近录入优先</small></div>
    <div class="list-body">${items.length ? items.map(caseCard).join('') : empty('没有符合条件的错题','可以换个日期、学科或错因试试。',true)}</div></section>
    <section id="detail" class="panel" aria-label="错题详情">${item ? detail(item) : empty('暂无可查看的题目','有搜索结果后，这里会显示原题和复习内容。')}</section>
  </div>`;
}
function caseCard(item) {
  const checked = item.id === state.selected;
  const stage = reviewStage(item);
  return `<button type="button" class="case-card" data-action="select-case" data-id="${html(item.id)}" aria-pressed="${checked}">
    <strong>${html(neutralTitle(item))}</strong><time datetime="${html(item.date)}">${shortDate(item.date)}</time>
    <span class="case-meta">${tag(item.subject,'subject')}${catalog.recordTags(item).slice(0,1).map(label => tag(label)).join('')}${tag(stage, dueForReview(item) && !reviewed(item) || item.ocrStatus === 'needs_review' ? 'warning' : 'blue')}</span>
    <span class="case-caret">${icon('chevron')}</span>
  </button>`;
}
const tabs = [['redo','复习原题','calendar'],['source','图文核对','scan'],['analysis','错因分析','light'],['practice','变式练习','branch'],['coach','逐题讲解','spark']];
function detail(item) {
  const count = reviewLog(item).wrong;
  return `<div class="detail-header"><div class="detail-meta">${tag(item.subject,'subject')}${tag(reviewStage(item),dueForReview(item) && !reviewed(item) ? 'warning' : 'blue')}${count ? tag(`复习答错 ${count} 次`,'warning') : ''}${item.captured ? tag(({auto:'高拍仪识别',voice:'语音录入',manual:'手动录入'})[item.captured] || '样张录入') : ''}<time datetime="${html(item.date)}">录入 ${showDate(item.date)}</time></div><h2>${html(neutralTitle(item))}</h2></div>
    <div class="detail-tabs" role="tablist" aria-label="错题详情页签">${tabs.map(([id,name,symbol]) => `<button type="button" role="tab" id="tab-${id}" aria-controls="detail-panel" aria-selected="${state.tab === id}" tabindex="${state.tab === id ? '0' : '-1'}" data-action="tab" data-tab="${id}">${icon(symbol)}${name}</button>`).join('')}</div>
    <div class="detail-body" id="detail-panel" role="tabpanel" aria-labelledby="tab-${state.tab}">${detailPanel(item)}</div>`;
}
function detailPanel(item) {
  if (state.tab === 'redo') return redoPanel(item);
  if (state.tab === 'analysis') return analysisPanel(item);
  if (state.tab === 'practice') return practicePanel(item.variant, item.id, 'case');
  if (state.tab === 'coach') return coachPanel(item);
  const verified = item.ocrStatus === 'verified';
  return `<div class="source-grid">
      <section class="source-card"><div class="card-cap"><strong>${icon('image')}原题裁图</strong>${tag('仅题目区域','subject')}</div>
        <button type="button" class="crop-frame" data-action="photo" aria-label="放大查看原题裁图">${photo(item)}</button></section>
      <section class="source-card"><div class="card-cap"><strong>${icon('type')}OCR 题干</strong>${tag(verified ? '已校对' : '待核对',verified ? 'subject' : 'warning')}</div>
        <div class="ocr-content"><p>${html(item.question)}</p>${action('edit','校对文字','edit','ghost')}</div></section>
    </div>
    <div class="detail-actions">${action('start-review','开始复习','calendar','primary')}${action('tab','查看错因','light','secondary','data-tab="analysis"')}</div>`;
}

function redoPanel(item) {
  const { latest } = reviewLog(item);
  const method = state.reviewMethod;
  const paper = method === 'paper';
  const status = latest && `<div class="feedback ${latest.outcome === 'incorrect' ? 'needs-review' : ''}"><strong>${latest.outcome === 'correct' ? '独立答对 · 待复测' : '上次复习答错 · 未掌握'}</strong><p>${html(latest.outcome === 'correct' ? '下次再独立做一道相关题。' : '可以再做一遍，或者接着听小知讲解。')}</p></div>`;
  return `<div class="inset review-question"><strong>先独立做一遍</strong><p>${html(item.question)}</p>${action('photo','核对原图（含旧作答）','image','ghost')}</div>
    ${status || ''}${method ? `<div class="redo-entry"><div class="section-title"><h3>${paper ? '草稿纸重做' : '语音讲解'}</h3>${tag('样例判定','blue')}</div><p class="section-copy">${paper ? '正式使用时，把草稿放到高拍仪下，做完再请小知检查。' : '正式使用时，直接告诉小知你怎么算的。'}</p><div class="detail-actions example-actions">${[['correct','样例作答一'],['incorrect','样例作答二'],['unclear',paper ? '模拟没拍清' : '模拟没听清']].map(([key,label])=>action('review-example',label,'','secondary',`data-example="${key}"`)).join('')}</div><label class="modal-field">${paper ? '高拍仪识别文字' : '语音转写'}<textarea id="review-draft" maxlength="500" rows="2" placeholder="选择上方样例，或输入要演示的内容">${html(state.reviewDraft)}</textarea></label><div class="detail-actions">${action('grade-review','请小知判断','check','primary',state.reviewDraft.trim() ? '' : 'disabled')}${action('cancel-review','换种方式','','ghost')}</div>${state.reviewMessage ? `<div class="feedback needs-review" role="status"><strong>请再试一次</strong><p>${html(state.reviewMessage)}</p></div>` : ''}</div>` : `<div class="detail-actions">${action('choose-method','在草稿纸上重做','scan','primary','data-mode="paper"')}${action('choose-method','用语音讲解','mic','secondary','data-mode="voice"')}</div><p class="flow-note">做完再让小知判断。需要时也可以直接去「逐题讲解」。</p>`}`;
}
function analysisPanel(item) {
  return `<div class="section-title"><h3>这道题可能卡在哪里</h3>${tag('示例分析','blue','spark')}</div>
    <p class="section-copy">${html(item.analysis.summary)}</p>
    ${item.analysis.causes.map(cause => `<div class="inset"><strong>${html(cause.label)}</strong><p>${html(cause.body)}</p></div>`).join('')}
    <div class="inset"><strong>错因标签</strong><div class="chips">${catalog.recordTags(item).map(label => tag(label,'subject')).join('')}${action('edit-tags','编辑标签','edit','ghost')}</div></div>
    <div class="inset"><strong>相关知识点</strong><div class="chips">${item.analysis.knowledge.map(k => tag(k,'subject')).join('')}</div></div>
    <div class="inset"><strong>订正步骤</strong><ol>${item.analysis.strategy.map(k => `<li>${html(k)}</li>`).join('')}</ol></div>
    <div class="detail-actions">${action('tab','练一道变式','branch','primary','data-tab="practice"')}${action('topic-jump','看同类错题','chart','secondary',`data-id="${html(item.typeId)}"`)}</div>`;
}
function practicePanel(p, id, kind) {
  const key = attemptKey(id,kind);
  const result = saved.attempts[key];
  const selected = state.drafts[key] ?? result?.choice ?? '';
  return `<div class="section-title"><h3>${kind === 'case' ? '换一种问法' : '专题巩固'}</h3>${tag('示例练习','blue','branch')}</div>
    <p class="practice-prompt">${html(p.prompt)}</p>
    <fieldset class="choice-set" aria-label="选择你的答案">${p.options.map(option => `<label class="choice"><input type="radio" name="practice" value="${html(option.key)}" data-attempt="${html(key)}" ${selected === option.key ? 'checked' : ''} ${result ? 'disabled' : ''}><b>${html(option.key)}.</b><span>${html(option.text)}</span></label>`).join('')}</fieldset>
    ${result ? `<div class="feedback ${result.correct ? '' : 'needs-review'}" role="status"><strong>${result.correct ? '本题答对 · 待复测' : '这题还差一步'}</strong><p>${html(result.correct ? p.explanation : p.hint)}</p></div>` : ''}
    <div class="detail-actions">${result ? action('retry-practice','再练一次','rotate','secondary',`data-kind="${kind}" data-id="${html(id)}"`) : action('submit-practice','提交答案','check','primary',`data-kind="${kind}" data-id="${html(id)}" ${selected ? '' : 'disabled'}`)}
    ${kind === 'case' ? action('tab','先听讲解','spark','ghost','data-tab="coach"') : ''}</div>`;
}
function coachPanel(item) {
  const guided = state.coachMode === 'socratic';
  const steps = item.analysis.strategy;
  const stage = Math.min(state.coachStep, steps.length - 1);
  const {latest, wrong} = reviewLog(item);
  return `<div class="section-title"><h3>把这题讲明白</h3>${tag('示例讲解','blue','spark')}</div>
    ${latest?.outcome === 'incorrect' ? `<div class="feedback needs-review" role="status"><strong>这次还没做对 · 累计答错 ${wrong} 次</strong><p>${html(latest.feedback)}</p></div>` : ''}
    <div class="segmented" aria-label="讲解方式"><button type="button" data-action="coach-mode" data-mode="socratic" aria-pressed="${guided}">一步步引导</button><button type="button" data-action="coach-mode" data-mode="direct" aria-pressed="${!guided}">直接讲解</button></div>
    ${guided ? `<div class="explanation"><strong>第 ${stage+1} 步 / 共 ${steps.length} 步</strong><p>${html(steps[stage])}</p></div>` : `<div class="explanation"><strong>解题思路</strong><ol>${steps.map(step => `<li>${html(step)}</li>`).join('')}</ol><p>参考答案 ${html(item.correct)}</p></div>`}
    <div class="detail-actions">${guided && stage < steps.length - 1 ? action('coach-next','下一步','arrow','primary') : action('tab','练一道变式','branch','primary','data-tab="practice"')}${action('start-review','回到原题重做','rotate','ghost')}</div>`;
}

function topicsView() {
  const cases = filterCases(allCases(), state.filters, false);
  const ranks = demo.topics.map(topic => ({...topic, related: cases.filter(item => item.typeId === topic.id)}))
    .filter(topic => topic.related.length).sort((a,b) => b.related.length - a.related.length);
  if (!ranks.some(topic => topic.id === state.topic)) { state.topic = ranks[0]?.id ?? null; state.topicStep = -1; }
  const topic = ranks.find(row => row.id === state.topic);
  const sampleCount = ranks.reduce((sum,row) => sum + row.related.length, 0);
  return `<div class="topics-grid"><section class="panel" aria-label="高频题型排行"><div class="list-head"><h2>按错题次数排序</h2><small>当前范围 · ${sampleCount} 道</small></div>
    <div class="list-body">${ranks.length ? ranks.map((row,i) => `<button type="button" class="topic-card" data-action="select-topic" data-id="${html(row.id)}" aria-pressed="${row.id === state.topic}"><span class="rank">${String(i+1).padStart(2,'0')}</span><span><strong>${html(row.title)}</strong><small>${html(row.subject)}</small></span><span class="topic-count">${row.related.length} 道错题</span></button>`).join('') : empty('当前范围暂无高频题型','调整筛选可以查看更多。',true)}</div></section>
    <section class="panel" aria-label="题型专题">${topic ? topicDetail(topic) : empty('暂无可查看的题型','筛选范围内有错题时，会显示可复习的专题。')}</section></div>`;
}
function topicDetail(topic) {
  const causes = unique(topic.related.flatMap(catalog.recordTags));
  const step = state.topicStep;
  return `<div class="detail-header"><div class="detail-meta">${tag(topic.subject,'subject')}${tag(`${topic.related.length} 道同类错题`,'blue')}</div><h2>${html(topic.title)}</h2></div>
    <div class="detail-body">
    ${step === topic.steps.length ? `${practicePanel(topic.practice,topic.id,'topic')}<div class="detail-actions">${action('topic-prev','回到讲解','book','ghost')}</div>` : `
      <div class="section-title"><h3>${step < 0 ? '同一类题，为什么反复错' : `专题讲解 · ${step+1} / ${topic.steps.length}`}</h3>${tag('示例讲解','blue','spark')}</div>
      ${step < 0 ? `<p class="section-copy">${html(topic.description)}</p><div class="chips">${causes.map(cause=>tag(cause,'warning')).join('')}</div><div class="inset"><strong>代表例题</strong><p>${html(topic.example)}</p></div>` : `<div class="topic-step"><div class="step-number">${step+1}</div><div><strong>${html(topic.steps[step].title)}</strong><p>${html(topic.steps[step].body)}</p></div></div>`}
      <div class="detail-actions">${action('topic-next',step < 0 ? '开始专题讲解' : step+1===topic.steps.length ? '开始专题练习' : '下一步','arrow','primary')}${step > 0 ? action('topic-prev','上一步','','secondary') : ''}</div>`}
      <div class="related"><span class="flow-note">代表错题</span>${topic.related.slice(0,4).map(item => action('open-related',item.title,'corner','secondary',`data-id="${html(item.id)}"`)).join('')}</div>
      <p class="flow-note">当前排行统计错题次数。计算错率需有同类题的总作答数。</p>
    </div>`;
}

function openModal(kind, trigger) {
  if (!dialog.open) restoreFocus = trigger || document.activeElement;
  state.modal = kind;
  renderModal();
  if (!dialog.open) dialog.showModal();
}
function modalView(title, content, footer = '') {
  return `<div class="modal-head"><h2 id="modal-title">${html(title)}</h2><button class="icon-button" type="button" data-action="close" aria-label="关闭">${icon('close')}</button></div><div class="modal-content">${content}</div>${footer ? `<div class="modal-foot">${footer}</div>` : ''}`;
}
function renderModal() {
  const item = currentItem();
  if (state.modal === 'photo' && item) modal.innerHTML = modalView('原题裁图',photo(item,'modal-photo'),action('close','关闭','','secondary'));
  if (state.modal === 'edit' && item) modal.innerHTML = modalView('校对 OCR 题干', `<p>对照原题裁图核对题号、符号和选项。</p>${photo(item,'capture-preview')}<label class="modal-field">题干文字<textarea id="edit-question" maxlength="1200" rows="4">${html(item.question)}</textarea></label>`, action('close','取消','','secondary')+action('save-edit','保存校对','check','primary'));
  if (state.modal === 'edit-tags' && item) modal.innerHTML = modalView('编辑错因标签',`<p>保存后，这道题的新标签就会出现在错因筛选和搜索联想里。</p><label class="modal-field">标签（用逗号分隔）<input id="cause-labels" type="text" maxlength="96" value="${html(catalog.recordTags(item).join('，'))}"></label>`,action('close','取消','','secondary')+action('save-tags','保存标签','check','primary'));
  if (state.modal === 'capture') modal.innerHTML = captureModal();
  if (state.modal === 'observe') modal.innerHTML = observationModal();
  if (state.modal === 'voice') modal.innerHTML = modalView('试一条语音指令', `<p>选一句来模拟小知识别语音后的操作。学习和错题本共用这一个入口。</p><div class="voice-examples">
    ${action('voice-example','小知，录入这道错题','mic','secondary','data-example="capture"')}
    ${action('voice-example','小知，打开错题本','mic','secondary','data-example="library"')}
    ${action('voice-example','小知，开始复习错题','mic','secondary','data-example="review"')}
    ${action('voice-example','小知，我说一遍这题怎么做','mic','secondary','data-example="explain"')}
    ${action('voice-example','小知，返回学习','mic','secondary','data-example="study"')}</div>`);
}

const observedCaseId = 'math-fractions-2';
function captureRecord(caseId) { return allCases().find(row => row.id === caseId || row.id === `capture-${caseId}`); }
function addCapture(caseId, question, answer, source) {
  const existing = captureRecord(caseId);
  if (existing) return existing.id;
  saved.imported = [{caseId, question, answer, source}, ...saved.imported];
  return `capture-${caseId}`;
}
function observationModal() {
  const paused = saved.paused[state.grade];
  const pending = saved.pending[state.grade];
  const supported = state.grade === '六年级';
  const item = demo.cases.find(row => row.id === observedCaseId);
  const existing = captureRecord(observedCaseId);
  const voice = state.captureSource === 'voice';
  const title = voice ? '小知已打开错题录入' : '高拍仪持续观察';
  return modalView(title,`<div class="section-title"><strong>${paused ? '观察已暂停' : '持续观察中'}</strong>${tag('设备演示','blue')}</div><p>${voice ? '请把题目和批改区域放到高拍仪下。' : '页面放稳后，系统识别题目、作答与对应的批改结果。'} 能确认错题时自动入册，只留题目裁图。题目或批改对应不清时，先保留待核对候选。</p>
    ${supported ? `<div class="inset"><strong>${existing ? '第 13 题已入册' : pending ? '有一条待核对候选' : '等待稳定页面'}</strong><p>${existing ? '再次识别到同一道题会复用已有记录。' : pending ? '批改痕迹与题目无法可靠对应，核对题目区域后再决定是否保存。' : '用下面的两个样例查看不同识别结果。'}</p></div>
    ${existing ? `${photo(item,'capture-preview')}<div class="detail-actions">${action('open-captured','打开已录入错题','arrow','primary',`data-id="${html(existing.id)}"`)}</div>` : pending ? `${photo(item,'capture-preview')}<div class="detail-actions">${action('confirm-region','去核对候选','scan','primary')}${action('dismiss-candidate','忽略候选','','ghost')}</div>` : `<div class="detail-actions">${action('observe-sample','模拟识别到明确批改','check','primary',`data-example="certain" ${paused ? 'disabled' : ''}`)}${action('observe-sample','模拟识别不清','scan','secondary',`data-example="unclear" ${paused ? 'disabled' : ''}`)}</div>`}` : '<p>本轮自动识别样例在六年级学档。</p>'}`,
  action('toggle-observation',paused ? '恢复观察' : '暂停观察','','ghost')+action('capture','手动框选','plus','secondary',supported ? '' : 'disabled'));
}
function captureModal() {
  const capture = demo.capture;
  if (state.grade !== '六年级') return modalView('手动录入纸面错题','<p>本轮可框选的试卷样张在六年级学档。</p>',action('close','关闭','','secondary'));
  const selected = capture.regions[state.captureIndex];
  const item = demo.cases.find(row => row.id === selected.caseId);
  const existing = captureRecord(selected.caseId);
  const progress = `<ol class="step-strip"><li ${state.captureStep === 1 ? 'aria-current="step"' : ''}>1. 框选题目</li><li ${state.captureStep === 2 ? 'aria-current="step"' : ''}>2. 核对并保存</li></ol>`;
  if (state.captureStep === 1) return modalView('录入纸面错题',`${progress}<p>在示例试卷中选择一道题。整页只用于选区，保存时仅保留选中的题目区域。</p><div class="capture-sheet"><img src="${html(capture.image)}" alt="包含两道题的合成试卷样张">${capture.regions.map((r,i)=>`<button type="button" class="capture-region" aria-label="框选第 ${i+1} 道题" aria-pressed="${i===state.captureIndex}" data-action="choose-crop" data-index="${i}" style="left:${r.x}%;top:${r.y}%;width:${r.w}%;height:${r.h}%"><span>${i===state.captureIndex ? '已选' : '选择'}第 ${i+1} 题</span></button>`).join('')}</div>`,action('close','取消','','secondary')+action('capture-next','预览裁图','arrow','primary'));
  if (existing) return modalView('这道题已在错题本', `${progress}<p>打开已有错题，可继续校对题干、查看错因和练习。</p>${photo(item,'capture-preview')}`, action('capture-back','返回选区','','secondary')+action('open-captured','查看已有错题','arrow','primary',`data-id="${html(existing.id)}"`));
  return modalView('核对并保存错题',`${progress}<p>${tag('仅保存所选题目','subject','check')} ${state.captureSource === 'candidate' ? '核对题号、作答与批改区域是否对应。小知会用重新对齐的批改标记判定。' : '核对题干和实际作答后保存。'}</p>${photo(item,'capture-preview')}<label class="modal-field">OCR 题干<textarea id="capture-question" maxlength="1200" rows="3">${html(item.question)}</textarea></label><label class="modal-field">当时作答<input type="text" maxlength="400" id="capture-answer" value="${html(item.answer)}"></label>`,action('capture-back','返回选区','','secondary')+action('save-crop',state.captureSource === 'candidate' ? '核对完成并保存' : '保存到错题本','check','primary'));
}

function handleAction(button) {
  const { action: name, id, tab, kind, mode, example } = button.dataset;
  if (name === 'close') { dialog.close(); return; }
  if (name === 'reset') { state.filters = blankFilters(); render(); document.getElementById('query')?.focus(); return; }
  if (name === 'open-book' || name === 'open-review') { state.filters = blankFilters(); go(name === 'open-book' ? 'library' : 'review'); return; }
  if (name === 'select-case') {
    state.selected = id; state.tab = 'redo'; state.coachStep = 0; resetAttempt(); renderResults();
    if (window.matchMedia('(max-width: 570px)').matches) document.getElementById('tab-redo')?.focus();
    else view.querySelector(`[data-action="select-case"][data-id="${id}"]`)?.focus();
    return;
  }
  if (name === 'tab') { state.tab = tab; renderResults(); document.getElementById(`tab-${tab}`)?.focus(); return; }
  if (name === 'start-review' || name === 'choose-method' || name === 'cancel-review') {
    resetAttempt(); state.tab = 'redo'; state.reviewMethod = mode || ''; renderResults();
    (mode ? document.getElementById('review-draft') : view.querySelector('[data-action="choose-method"]'))?.focus(); return;
  }
  if (name === 'review-example') {
    const item = currentItem();
    if (!item) return;
    state.reviewDraft = review.getExamples(item)[state.reviewMethod][example];
    state.reviewMessage = ''; renderResults(); view.querySelector('[data-action="grade-review"]')?.focus(); return;
  }
  if (name === 'grade-review') {
    const item = currentItem();
    if (!item || !state.reviewDraft.trim()) return;
    const result = review.assess(item,state.reviewMethod,state.reviewDraft);
    if (result.outcome === 'unclear') { state.reviewMessage = result.feedback; renderResults(); document.getElementById('review-draft')?.focus(); return; }
    const log = reviewLog(item);
    saved.reviews[item.id] = {wrong: log.wrong + (result.outcome === 'incorrect' ? 1 : 0), latest:{...result,method:state.reviewMethod}};
    if (result.outcome === 'incorrect') { state.tab = 'coach'; state.coachStep = 0; state.coachMode = 'socratic'; }
    resetAttempt(); render(); document.getElementById(`tab-${state.tab}`)?.focus();
    announceSaved(result.outcome === 'incorrect' ? '本次复习已记录，开始逐步讲解。' : '独立答对，已标记待复测。'); return;
  }
  if (name === 'observe') { state.captureSource = 'auto'; openModal('observe',button); return; }
  if (name === 'toggle-observation') {
    saved.paused[state.grade] = !saved.paused[state.grade]; save(); render(); renderModal();
    modal.querySelector('[data-action="toggle-observation"]')?.focus(); return;
  }
  if (name === 'observe-sample') {
    if (saved.paused[state.grade] || state.grade !== '六年级') return;
    const item = demo.cases.find(row => row.id === observedCaseId);
    if (example === 'unclear') saved.pending[state.grade] = item.id;
    else { addCapture(item.id,item.question,item.answer,'auto'); delete saved.pending[state.grade]; }
    save(); render(); renderModal(); modal.querySelector('[data-action="open-captured"], [data-action="confirm-region"]')?.focus(); return;
  }
  if (name === 'dismiss-candidate') { delete saved.pending[state.grade]; save(); render(); renderModal(); modal.querySelector('[data-action="observe-sample"]')?.focus(); return; }
  if (name === 'confirm-region') {
    state.captureIndex = demo.capture.regions.findIndex(row => row.caseId === saved.pending[state.grade]);
    if (state.captureIndex < 0) return;
    state.captureSource = 'candidate'; state.captureStep = 1; openModal('capture',button); return;
  }
  if (name === 'capture') {
    state.captureIndex = Math.max(0,demo.capture.regions.findIndex(region => !captureRecord(region.caseId)));
    state.captureSource = state.modal === 'observe' && state.captureSource === 'voice' ? 'voice' : 'manual';
    state.captureStep = 1; openModal('capture',button); return;
  }
  if (['photo','edit','edit-tags','voice'].includes(name)) { openModal(name,button); return; }
  if (name === 'save-tags') {
    const labels = document.getElementById('cause-labels').value.split(/[，,、]/).map(label => label.trim()).filter(Boolean);
    saved.labels[state.selected] = unique(labels).slice(0,8);
    if (!allCases().some(row => catalog.recordTags(row).includes(state.filters.cause))) state.filters.cause = '';
    dialog.close(); render(); announceSaved('标签已更新，筛选和搜索联想已同步。'); return;
  }
  if (name === 'save-edit') {
    const field = document.getElementById('edit-question');
    const question = field.value.trim();
    if (!question) { field.focus(); announce('请填写题干文字。'); return; }
    saved.edits[state.selected] = { question };
    dialog.close(); renderResults(); announceSaved('题干已校对，原题裁图可继续核对。'); return;
  }
  if (name === 'choose-crop') { state.captureIndex = Number(button.dataset.index); renderModal(); modal.querySelector('[aria-pressed="true"]')?.focus(); return; }
  if (name === 'capture-next' || name === 'capture-back') { state.captureStep = name === 'capture-next' ? 2 : 1; renderModal(); modal.querySelector('textarea,[aria-pressed="true"]')?.focus(); return; }
  if (name === 'open-captured') { state.selected = id; state.filters = blankFilters(); dialog.close(); go('library'); return; }
  if (name === 'save-crop') {
    const field = document.getElementById('capture-question');
    const question = field.value.trim();
    if (!question) { field.focus(); announce('请填写题干文字。'); return; }
    const caseId = demo.capture.regions[state.captureIndex].caseId;
    state.selected = addCapture(caseId, question, document.getElementById('capture-answer').value.trim(), state.captureSource === 'candidate' ? 'auto' : state.captureSource);
    if (saved.pending[state.grade] === caseId) delete saved.pending[state.grade];
    state.filters = blankFilters(); dialog.close(); go('library');
    announceSaved('已保存所选裁图和校对后的题干。'); return;
  }
  if (name === 'submit-practice') {
    const key = attemptKey(id,kind), choice = state.drafts[key];
    const p = kind === 'case' ? currentItem()?.variant : currentTopic()?.practice;
    if (!p || !p.options.some(option => option.key === choice)) return;
    saved.attempts[key] = { choice, correct: choice === p.correct };
    render(); announceSaved(choice === p.correct ? '变式答对，结果已保存。' : '作答已保存，可以看提示再练一次。'); return;
  }
  if (name === 'retry-practice') { const key = attemptKey(id,kind); delete saved.attempts[key]; delete state.drafts[key]; save(); render(); return; }
  if (name === 'coach-mode' || name === 'coach-next') {
    if (mode) { state.coachMode = mode; state.coachStep = 0; }
    else state.coachStep += 1;
    renderResults(); return;
  }
  if (name === 'select-topic' || name === 'topic-jump') {
    state.topic = id; state.topicStep = -1;
    if (name === 'topic-jump') { state.filters = blankFilters(); go('topics'); }
    else { renderResults(); view.querySelector(`[data-action="select-topic"][data-id="${id}"]`)?.focus(); }
    return;
  }
  if (name === 'topic-next' || name === 'topic-prev') { state.topicStep += name === 'topic-next' ? 1 : -1; renderResults(); return; }
  if (name === 'open-related') { state.selected = id; state.filters = { ...blankFilters(), topic: state.topic }; go('library'); return; }
  if (name === 'voice-example') {
    if (example === 'capture') { state.captureSource = 'voice'; openModal('observe',button); return; }
    dialog.close(); state.filters = blankFilters();
    if (example === 'explain') { go('library'); state.reviewMethod = 'voice'; renderResults(); document.getElementById('review-draft')?.focus(); }
    else go(example);
  }
}

function applyFilter(input) {
  const key = input.dataset.filter;
  state.filters[key] = input.value;
  closeSuggestions();
  if (key === 'date') { render(); view.querySelector('[data-filter="date"]')?.focus(); }
  else renderResults();
}
document.getElementById('app').addEventListener('click', event => {
  const nav = event.target.closest('[data-nav]');
  if (nav) { event.preventDefault(); go(nav.dataset.nav); return; }
  const button = event.target.closest('button[data-action]');
  if (button) handleAction(button);
});
document.getElementById('sample-grade').addEventListener('change', event => {
  state.grade = event.target.value; state.filters = blankFilters(); state.selected = null; state.topicStep = -1;
  saved.activeGrade = state.grade; save();
  go(state.page);
});
view.addEventListener('input', event => {
  const input = event.target;
  if (input.id === 'query') { state.searchOpen = true; state.suggestionIndex = -1; updateSuggestions(); }
  if (input.id === 'review-draft') {
    state.reviewDraft = input.value;
    view.querySelector('[data-action="grade-review"]').disabled = !input.value.trim();
  }
  if (input.id === 'study-notes') {
    saved.sessionNotes[state.grade] = input.value;
    document.getElementById('note-status').textContent = save() ? '便签已保存在当前示例会话' : '浏览器存储不可用，便签暂留在本页';
  }
});
view.addEventListener('focusin', event => { if (event.target.id === 'query') { state.searchOpen = true; updateSuggestions(); } });
view.addEventListener('focusout', event => {
  if (event.target.closest('.search-form') && !event.relatedTarget?.closest('.search-form')) closeSuggestions();
});
view.addEventListener('pointerdown', event => { if (event.target.closest('[data-pick]')) event.preventDefault(); });
view.addEventListener('click', event => { const pick = event.target.closest('[data-pick]'); if (pick) runSearch(Number(pick.dataset.pick)); });
view.addEventListener('submit', event => {
  if (event.target.id === 'search-form') { event.preventDefault(); runSearch(state.suggestionIndex); }
});
view.addEventListener('change', event => {
  if (event.target.dataset.filter) applyFilter(event.target);
  const key = event.target.dataset.attempt;
  if (key) { state.drafts[key] = event.target.value; view.querySelector('[data-action="submit-practice"]').disabled = false; }
});
view.addEventListener('keydown', event => {
  if (event.target.id === 'query' && ['Escape','ArrowDown','ArrowUp'].includes(event.key)) {
    if (event.key === 'Escape') { if (state.searchOpen) { event.preventDefault(); closeSuggestions(); } return; }
    const count = searchChoices(event.target.value).length;
    if (!count) return;
    event.preventDefault(); state.searchOpen = true;
    state.suggestionIndex = event.key === 'ArrowDown' ? (state.suggestionIndex+1) % count : (state.suggestionIndex <= 0 ? count-1 : state.suggestionIndex-1);
    updateSuggestions(); return;
  }
  if (event.target.getAttribute('role') !== 'tab' || !['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
  const index = tabs.findIndex(([id]) => id === state.tab);
  const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length-1 : (index + (event.key === 'ArrowLeft' ? tabs.length-1 : 1)) % tabs.length;
  event.preventDefault(); state.tab = tabs[next][0]; renderResults(); document.getElementById(`tab-${state.tab}`)?.focus();
});
dialog.addEventListener('close', () => { state.modal = null; if (restoreFocus?.isConnected) restoreFocus.focus(); restoreFocus = null; });
document.querySelectorAll('[data-icon]').forEach(node => { node.innerHTML = icons[node.dataset.icon] || ''; });
render();

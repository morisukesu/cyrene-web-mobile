/* ============================================================
   昔涟 · 手机版 Web 前端主逻辑
   分两块：聊天核心 / 设置抽屉
   安全不变量：Markdown 渲染不透传原始 HTML，渲染后再过一遍 sanitize
   ============================================================ */
(function () {
'use strict';

var $ = function (id) { return document.getElementById(id); };
var chat = $('chat'), input = $('input'), sendBtn = $('send-btn'),
    sidebar = $('sidebar'), scrim = $('scrim'), sessionList = $('session-list'),
    statusDot = $('status-dot'), modelBadge = $('model-badge'),
    chatTitle = $('chat-title'), modelInfo = $('model-info'),
    modeBtn = $('mode-btn'), modeBtnIcon = $('mode-btn-icon'),
    modeBtnLabel = $('mode-btn-label'), modeMenu = $('mode-menu'),
    settingsEl = $('settings'), settingsScrim = $('settings-scrim'),
    settingsBody = $('settings-body'), settingsNav = $('settings-nav'),
    saveStatus = $('save-status');

var currentSid = null, busy = false, aborter = null, timer = null, secs = 0, t0 = 0;
var CODE_STORE = {}, CODE_SEQ = 0;
var SETTINGS = null;                 // 服务端配置的本地镜像
var MODES = [], DEFAULT_MODE = 'chat', currentMode = 'chat';
var isMobile = function () { return window.matchMedia('(max-width:768px)').matches; };

/* ================= 工具函数 ================= */
function escHtml(s) {
  return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function api(path, opts) {
  return fetch(path, opts).then(function (r) {
    return r.json().catch(function () { return {}; }).then(function (b) {
      if (!r.ok) {
        var e = new Error((b && b.error) || ('HTTP ' + r.status));
        e.status = r.status; e.payload = b; throw e;
      }
      return b;
    });
  });
}
function apiPost(path, body) {
  return api(path, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {})
  });
}

/* ================= Markdown ================= */
/* 渲染后净化：marked 的 html token 已被转义，这里是第二道防线 */
var BAD_TAGS = {
  SCRIPT: 1, IFRAME: 1, OBJECT: 1, EMBED: 1, LINK: 1, META: 1, BASE: 1,
  FORM: 1, STYLE: 1, SVG: 1, MATH: 1, AUDIO: 1, VIDEO: 1, SOURCE: 1, TRACK: 1
};
var BAD_URL = /^\s*(javascript|vbscript|data)\s*:/i;
var URL_ATTRS = { href: 1, src: 1, 'xlink:href': 1, action: 1, formaction: 1 };

function sanitize(root) {
  var nodes = root.querySelectorAll('*'), i, el, attrs, j, name, v;
  for (i = 0; i < nodes.length; i++) {
    el = nodes[i];
    if (BAD_TAGS[el.tagName]) { el.remove(); continue; }
    if (el.tagName === 'INPUT' && el.type !== 'checkbox') { el.remove(); continue; }
    attrs = Array.prototype.slice.call(el.attributes);
    for (j = 0; j < attrs.length; j++) {
      name = attrs[j].name; v = attrs[j].value || '';
      if (/^on/i.test(name)) { el.removeAttribute(name); continue; }
      if (URL_ATTRS[name] && BAD_URL.test(v)) el.removeAttribute(name);
    }
    if (el.tagName === 'A') {
      el.setAttribute('target', '_blank');
      el.setAttribute('rel', 'noreferrer noopener');
    }
  }
  var tables = root.querySelectorAll('table'), k, wrap;
  for (k = 0; k < tables.length; k++) {
    if (tables[k].parentNode && tables[k].parentNode.classList
        && tables[k].parentNode.classList.contains('md-table-wrap')) continue;
    wrap = document.createElement('div'); wrap.className = 'md-table-wrap';
    tables[k].parentNode.insertBefore(wrap, tables[k]);
    wrap.appendChild(tables[k]);
  }
}

var mdReady = false;
if (window.marked) {
  try {
    marked.use({
      gfm: true, breaks: false, pedantic: false,
      renderer: {
        /* 原始 HTML 一律转义成可见文本，绝不透传 */
        html: function (token) { return escHtml((token && token.text) || ''); },
        code: function (token) {
          var text = (token && token.text) || '';
          var lang = (((token && token.lang) || '').trim().split(/\s+/)[0] || '');
          var id = 'cb' + (++CODE_SEQ);
          CODE_STORE[id] = text;
          var body = '', hl = window.hljs;
          if (hl) {
            try {
              if (lang && hl.getLanguage && hl.getLanguage(lang)) {
                body = hl.highlight(text, { language: lang, ignoreIllegals: true }).value;
              } else if (text.length < 3000 && hl.highlightAuto) {
                body = hl.highlightAuto(text).value;
              }
            } catch (e) { body = ''; }
          }
          if (!body) body = escHtml(text);
          return '<div class="md-code"><div class="md-code-bar">'
            + '<span class="md-code-lang">' + (escHtml(lang) || 'text') + '</span>'
            + '<button type="button" class="md-copy" data-cb="' + id + '">复制</button>'
            + '</div><pre><code' + (lang ? ' class="language-' + escHtml(lang) + '"' : '')
            + '>' + body + '</code></pre></div>';
        },
        checkbox: function (token) {
          return '<input type="checkbox" disabled' + ((token && token.checked) ? ' checked' : '') + '>';
        }
      }
    });
    mdReady = true;
  } catch (e) { mdReady = false; }
}

function mountMd(el, text) {
  var src = String(text == null ? '' : text);
  if (!mdReady) { el.textContent = src; el.classList.add('md-fallback'); return; }
  try {
    var tpl = document.createElement('template');
    tpl.innerHTML = marked.parse(src);
    sanitize(tpl.content);
    el.innerHTML = '';
    while (tpl.content.firstChild) el.appendChild(tpl.content.firstChild);
    el.classList.add('md');
    var boxes = el.querySelectorAll('.md-task input[type=checkbox]'), i;
    for (i = 0; i < boxes.length; i++) {
      if (boxes[i].parentNode) boxes[i].parentNode.classList.add('md-task');
    }
  } catch (e) {
    el.textContent = src; el.classList.add('md-fallback');
  }
}

/* Markdown → 纯文本，用于 TTS 朗读 */
function mdToPlain(src) {
  var s = String(src == null ? '' : src);
  s = s.replace(/```[\s\S]*?```/g, ' 代码块已省略。 ');
  s = s.replace(/`([^`]*)`/g, '$1');
  s = s.replace(/^\s{0,3}#{1,6}\s+/gm, '');
  s = s.replace(/^\s{0,3}>\s?/gm, '');
  s = s.replace(/^\s*[-*+]\s+\[[ xX]\]\s*/gm, '');
  s = s.replace(/^\s*[-*+]\s+/gm, '');
  s = s.replace(/^\s*\d+[.)]\s+/gm, '');
  s = s.replace(/^\s*\|.*\|\s*$/gm, '');
  s = s.replace(/^\s*([-*_])\s*(\1\s*){2,}$/gm, '');
  s = s.replace(/!?\[([^\]]*)\]\([^)]*\)/g, '$1');
  s = s.replace(/[*_~]{1,3}/g, '');
  return s.replace(/\n{2,}/g, '\n').trim();
}

/* ================= 回复分段 =================
   桌面端 mobileMessageSegmentation：长回复按语义拆成多个气泡。
   只在段落边界切，且跳过围栏代码块内部，避免把代码切断。 */
function segmentReply(text, on) {
  var src = String(text == null ? '' : text);
  if (!on) return [src];
  var lines = src.split('\n'), chunks = [], cur = [], inFence = false, i, ln;
  for (i = 0; i < lines.length; i++) {
    ln = lines[i];
    if (/^\s*(```|~~~)/.test(ln)) inFence = !inFence;
    if (!inFence && ln.trim() === '' && cur.length) {
      /* 空行是段落边界；连续空行只切一次 */
      var joined = cur.join('\n').trim();
      if (joined) chunks.push(joined);
      cur = [];
      continue;
    }
    cur.push(ln);
  }
  if (cur.length) { var tail = cur.join('\n').trim(); if (tail) chunks.push(tail); }
  if (chunks.length <= 1) return [src];
  /* 太短的碎块合并回去，避免一句话一个气泡 */
  var merged = [], buf = '';
  for (i = 0; i < chunks.length; i++) {
    if (buf.length && (buf.length + chunks[i].length) < 40) { buf += '\n\n' + chunks[i]; continue; }
    if (buf) merged.push(buf);
    buf = chunks[i];
  }
  if (buf) merged.push(buf);
  return merged.length > 1 ? merged : [src];
}

/* ================= 侧边栏 ================= */
function setSidebar(open) {
  sidebar.classList.toggle('hidden', !open);
  scrim.classList.toggle('on', !!open && isMobile());
}
$('menu-btn').addEventListener('click', function () {
  setSidebar(sidebar.classList.contains('hidden'));
});
$('sidebar-close').addEventListener('click', function () { setSidebar(false); });
scrim.addEventListener('click', function () { setSidebar(false); });

/* ================= 输入框 ================= */
input.addEventListener('input', function () {
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 140) + 'px';
});
input.addEventListener('keydown', function (e) {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); onSend(); }
});

/* ================= 渲染 ================= */
function makeBubble(cls, cont) {
  var row = document.createElement('div');
  row.className = 'msg-row ' + cls + (cont ? ' cont' : '');
  var av = document.createElement('div'); av.className = 'avatar ' + cls;
  av.textContent = cls === 'bot' ? '♪' : '你';
  var bub = document.createElement('div'); bub.className = 'bubble ' + cls;
  if (cls === 'bot') { row.appendChild(av); row.appendChild(bub); }
  else { row.appendChild(bub); row.appendChild(av); }
  return { row: row, bub: bub };
}

/* reasoning 折叠块：默认收起，点击 header 展开。
   正文走 mountMd → sanitize，与主回复同一条安全路径，不透传原始 HTML。 */
function makeReasoning(text) {
  var block = document.createElement('div'); block.className = 'reasoning-block';
  var head = document.createElement('div'); head.className = 'reasoning-head';
  head.setAttribute('role', 'button'); head.setAttribute('tabindex', '0');
  head.setAttribute('aria-expanded', 'false');
  head.innerHTML = '<span class="reasoning-caret">\u25B8</span><span>\uD83D\uDCAD 思考过程</span>';
  var body = document.createElement('div'); body.className = 'reasoning-body';
  mountMd(body, text);
  var toggle = function () {
    var open = block.classList.toggle('open');
    head.setAttribute('aria-expanded', open ? 'true' : 'false');
  };
  head.addEventListener('click', toggle);
  head.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); }
  });
  block.appendChild(head); block.appendChild(body);
  return block;
}

function atBottom() { return chat.scrollHeight - chat.scrollTop - chat.clientHeight < 160; }
function scrollBottom(force) { if (force || atBottom()) chat.scrollTop = chat.scrollHeight; }

/* ================= 工作笔记（update_todo）=================
   后端把 session.todos 随 /chat/{sid} 与 /send 一起回传，这里渲染成
   输入框上方的常驻进度条。设计取舍：
   · 不塞进气泡里 —— todo 是「当前任务状态」，会随轮次整表替换，
     塞进气泡就会每轮多一坨重复内容，越滚越长。
   · 默认收起只显示一行摘要（3/7 完成），点开才看全表。手机上寸土寸金。
   · 全部 completed/cancelled 时自动收起并淡化，任务结束了就别再占视线。 */
var TODO_ICONS = { pending: '○', in_progress: '◐', completed: '●', cancelled: '✕' };

function todoBar() {
  var el = $('todo-bar');
  if (el) return el;
  el = document.createElement('div');
  el.id = 'todo-bar';
  el.hidden = true;
  /* 插在 #chat 之前：topbar 之下、消息流之上，滚动消息时进度条不动 */
  chat.parentNode.insertBefore(el, chat);
  return el;
}

function todosDone(items) {
  return items.filter(function (t) {
    return t.status === 'completed' || t.status === 'cancelled';
  }).length;
}

function renderTodos(items) {
  var bar = todoBar();
  items = Array.isArray(items) ? items : [];
  if (!items.length) { bar.hidden = true; bar.innerHTML = ''; return; }

  var done = todosDone(items), allDone = done === items.length;
  var running = items.filter(function (t) { return t.status === 'in_progress'; })[0];
  bar.hidden = false;
  bar.innerHTML = '';
  bar.classList.toggle('is-done', allDone);

  var head = document.createElement('button');
  head.type = 'button';
  head.className = 'todo-bar__head';
  head.setAttribute('aria-expanded', String(!allDone));
  var label = allDone
    ? ('✓ 任务完成 ' + done + '/' + items.length)
    : ('📝 ' + done + '/' + items.length + ' · ' + (running ? running.content : '待办 ' + (items.length - done) + ' 项'));
  head.textContent = label;
  bar.appendChild(head);

  var body = document.createElement('div');
  body.className = 'todo-bar__body';
  body.hidden = allDone;          /* 做完了默认收起，想看再点 */
  items.forEach(function (t) {
    var li = document.createElement('div');
    li.className = 'todo-item todo-item--' + (t.status || 'pending');
    var ic = document.createElement('span');
    ic.className = 'todo-item__icon';
    ic.textContent = TODO_ICONS[t.status] || '○';
    var tx = document.createElement('span');
    tx.className = 'todo-item__text';
    tx.textContent = t.status === 'in_progress' && t.activeForm ? t.activeForm : t.content;
    li.appendChild(ic); li.appendChild(tx);
    body.appendChild(li);
  });
  bar.appendChild(body);

  head.addEventListener('click', function () {
    body.hidden = !body.hidden;
    head.setAttribute('aria-expanded', String(!body.hidden));
  });
}

/* ================= 结构化提问（ask_user）=================
   后端 endReason=awaiting_user 时带 ask={questions:[...]}。
   渲染成可点选的卡片；用户答完点「提交回答」，答案拼成文本走正常发送通道
   （后端 _handle_send 收到新 user 消息会清掉 session.pendingAsk）。
   不做单独的 /ask/answer 端点：回答本质就是一条用户消息，
   多一个端点只会让历史和重试逻辑分叉。 */
var ASK_KIND_LABEL = { single_select: '单选', multi_select: '多选', text: '填写' };

function makeAskCard(ask, interactive) {
  var qs = (ask && ask.questions) || [];
  var p = makeBubble('bot');
  p.bub.classList.add('bubble--ask');
  if (!qs.length) return p;

  var box = document.createElement('div'); box.className = 'ask-box';
  var title = document.createElement('div'); title.className = 'ask-box__title';
  title.textContent = '❓ 需要你定一下';
  box.appendChild(title);

  var answers = {};                 /* qid → [value] 或 string */
  var groups = [];

  qs.forEach(function (q, qi) {
    var g = document.createElement('div'); g.className = 'ask-q';
    var h = document.createElement('div'); h.className = 'ask-q__head';
    var idx = document.createElement('span'); idx.className = 'ask-q__idx';
    idx.textContent = 'Q' + (qi + 1);
    var lab = document.createElement('span'); lab.className = 'ask-q__label';
    lab.textContent = q.question || '';
    var kind = document.createElement('span'); kind.className = 'ask-q__kind';
    kind.textContent = ASK_KIND_LABEL[q.type] || q.type || '';
    h.appendChild(idx); h.appendChild(lab); h.appendChild(kind);
    g.appendChild(h);

    answers[q.id] = q.type === 'multi_select' ? [] : '';

    if (q.type === 'text' || !(q.options || []).length) {
      var ta = document.createElement('textarea');
      ta.className = 'ask-q__text';
      ta.rows = 2;
      ta.placeholder = '写点什么…';
      ta.disabled = !interactive;
      ta.addEventListener('input', function () { answers[q.id] = ta.value; syncSubmit(); });
      g.appendChild(ta);
    } else {
      var wrap = document.createElement('div');
      wrap.className = 'ask-q__opts' + (q.type === 'multi_select' ? ' is-multi' : '');
      (q.options || []).forEach(function (o) {
        var b = document.createElement('button');
        b.type = 'button';
        b.className = 'ask-opt';
        b.disabled = !interactive;
        b.setAttribute('aria-pressed', 'false');
        var nm = document.createElement('span'); nm.className = 'ask-opt__label';
        nm.textContent = o.label;
        b.appendChild(nm);
        if (o.description) {
          var ds = document.createElement('span'); ds.className = 'ask-opt__desc';
          ds.textContent = o.description;
          b.appendChild(ds);
        }
        b.addEventListener('click', function () {
          if (q.type === 'multi_select') {
            var arr = answers[q.id] || [];
            var at = arr.indexOf(o.value);
            if (at >= 0) arr.splice(at, 1); else arr.push(o.value);
            answers[q.id] = arr;
            b.classList.toggle('is-on', arr.indexOf(o.value) >= 0);
            b.setAttribute('aria-pressed', String(arr.indexOf(o.value) >= 0));
          } else {
            answers[q.id] = o.value;
            /* 单选：同组其他按钮复位，靠 DOM 扫，不用记状态 */
            wrap.querySelectorAll('.ask-opt').forEach(function (x) {
              x.classList.remove('is-on'); x.setAttribute('aria-pressed', 'false');
            });
            b.classList.add('is-on'); b.setAttribute('aria-pressed', 'true');
          }
          syncSubmit();
        });
        wrap.appendChild(b);
      });
      g.appendChild(wrap);
    }
    groups.push(g);
    box.appendChild(g);
  });

  var acts = document.createElement('div'); acts.className = 'ask-box__acts';
  var sub = document.createElement('button');
  sub.type = 'button'; sub.className = 'btn btn--primary btn--sm';
  sub.textContent = '提交回答';
  sub.disabled = true;
  acts.appendChild(sub);
  if (!interactive) {
    var stale = document.createElement('span');
    stale.className = 'ask-box__stale';
    stale.textContent = '（这条提问已经答过了）';
    acts.appendChild(stale);
  }
  box.appendChild(acts);

  function allAnswered() {
    return qs.every(function (q) {
      var v = answers[q.id];
      if (Array.isArray(v)) return v.length > 0;
      return typeof v === 'string' && v.trim() !== '';
    });
  }
  function syncSubmit() { sub.disabled = !interactive || !allAnswered(); }

  function labelOf(q, val) {
    var o = (q.options || []).filter(function (x) { return x.value === val; })[0];
    return o ? o.label : val;
  }

  sub.addEventListener('click', function () {
    if (!allAnswered()) return;
    var lines = qs.map(function (q) {
      var v = answers[q.id];
      var txt = Array.isArray(v) ? v.map(function (x) { return labelOf(q, x); }).join('、')
                                 : String(v).trim();
      return (q.question || q.id) + '：' + txt;
    });
    /* 卡片就地定格：撤掉交互、标记已答，避免用户重复提交同一个问题 */
    interactive = false;
    syncSubmit();
    box.querySelectorAll('textarea, .ask-opt').forEach(function (x) { x.disabled = true; });
    sub.textContent = '已提交';
    var stick = atBottom();
    addBubble(lines.join('\n'), 'user');
    scrollBottom(stick);
    sendTurn(lines.join('\n'), false);
  });

  p.bub.appendChild(box);
  return p;
}

/* 错误气泡：LLM 调用失败时用。与正文气泡分开渲染，不走 mountMd，
   避免 "[API 554]" 这类串被当 Markdown 解析，也避免被存成正文。
   带「重试」按钮，点了拿最近一条 user 消息重发。 */
function makeErrorBubble(errText) {
  var p = makeBubble('bot');
  p.bub.classList.add('bubble--error');
  var box = document.createElement('div'); box.className = 'err-box';
  var t = document.createElement('div'); t.className = 'err-box__text';
  t.textContent = '⚠ ' + (errText || '请求失败');
  box.appendChild(t);
  var acts = document.createElement('div'); acts.className = 'err-box__acts';
  var rb = document.createElement('button');
  rb.type = 'button'; rb.className = 'btn btn--ghost btn--sm'; rb.textContent = '重试';
  rb.addEventListener('click', function () { retryLast(); });
  acts.appendChild(rb);
  box.appendChild(acts);
  p.bub.appendChild(box);
  return p;
}

/* 找最近一条 user 消息重新发一次。失败轮次的 user 消息仍在库里，
   所以走 retry 通道：前端不再画用户气泡，后端也不再追加 user 消息，
   只把那条错误空壳换成新的回复。 */
function retryLast() {
  if (busy) return;
  var rows = chat.querySelectorAll('.msg-row.user .bubble');
  if (!rows.length) return;
  var text = rows[rows.length - 1].textContent;
  if (!text) return;
  /* 摘掉末尾那个错误气泡，避免重试后错误提示和正确回复并存。
     DOM 结构是 #chat > .msg-row.bot > .bubble--error，
     要删的是 .msg-row（= parentNode）。
     旧实现写了 parentNode.parentNode.remove()，删的是 #chat 整个聊天容器，
     表现为「重试后输入框跑到最上方」——聊天区被整个移除了。 */
  var errBubs = chat.querySelectorAll('.msg-row.bot .bubble--error');
  if (errBubs.length) {
    var row = errBubs[errBubs.length - 1].parentNode;
    if (row && row.parentNode === chat) row.remove();
  }
  sendTurn(text, true);
}

function addBubble(text, cls, toolInfo) {
  var stick = atBottom();
  var p = makeBubble(cls);
  if (cls === 'bot') mountMd(p.bub, text); else p.bub.textContent = text;
  if (toolInfo) {
    var t = document.createElement('div'); t.className = 'tool-info'; t.textContent = toolInfo;
    p.bub.appendChild(t);
  }
  chat.appendChild(p.row);
  scrollBottom(stick);
  return p;
}

/* 分段回复：第一段带头像，后续段用 cont 隐藏头像，视觉上仍是一个人的连续发言 */
function addSegmented(text, reasoning) {
  var seg = SETTINGS && SETTINGS.appearance
    && SETTINGS.appearance.mobileMessageSegmentation === 'on';
  var parts = segmentReply(text, seg), i, p, stick = atBottom();
  for (i = 0; i < parts.length; i++) {
    p = makeBubble('bot', i > 0);
    mountMd(p.bub, parts[i]);
    if (i === 0 && reasoning) p.bub.insertBefore(makeReasoning(reasoning), p.bub.firstChild);
    chat.appendChild(p.row);
  }
  scrollBottom(stick);
}

function showThinking() {
  hideThinking();
  var row = document.createElement('div'); row.className = 'msg-row bot'; row.id = 'thinking-row';
  var av = document.createElement('div'); av.className = 'avatar bot'; av.textContent = '♪';
  var box = document.createElement('div'); box.className = 'thinking-box';
  box.innerHTML = '<span class="dots"><span></span><span></span><span></span></span>'
    + '<span>昔涟正在想</span><span id="elapsed">0s</span>';
  row.appendChild(av); row.appendChild(box); chat.appendChild(row);
  scrollBottom(true);
  secs = 0;
  timer = setInterval(function () {
    secs++;
    var e = $('elapsed'); if (e) e.textContent = secs + 's';
  }, 1000);
}
function hideThinking() {
  if (timer) { clearInterval(timer); timer = null; }
  var r = $('thinking-row'); if (r) r.remove();
}
function setStatus(kind) {
  statusDot.classList.remove('thinking', 'error');
  if (kind) statusDot.classList.add(kind);
}

/* ================= 模式切换器 =================
   顶栏 mode-btn 点开下拉，选一个模式 → POST /chat/{sid}/mode。
   模式绑在会话上；没有会话时只改本地 currentMode，新建时再带上。 */
function findMode(id) {
  var i;
  for (i = 0; i < MODES.length; i++) { if (MODES[i].id === id) return MODES[i]; }
  return null;
}
function applyModeUI(mode) {
  var m = findMode(mode) || { id: mode, label: mode, icon: '💬' };
  if (modeBtnIcon) modeBtnIcon.textContent = m.icon || '💬';
  if (modeBtnLabel) modeBtnLabel.textContent = m.label || mode;
}
function closeModeMenu() {
  if (!modeMenu) return;
  modeMenu.classList.remove('open');
  modeMenu.innerHTML = '';
  if (modeBtn) modeBtn.setAttribute('aria-expanded', 'false');
}
function renderModeMenu() {
  if (!modeMenu) return;
  modeMenu.innerHTML = '';
  MODES.forEach(function (m) {
    var it = document.createElement('div');
    it.className = 'mode-item' + (m.id === currentMode ? ' current' : '');
    it.setAttribute('role', 'menuitem');
    it.setAttribute('data-mode', m.id);
    var ic = document.createElement('span'); ic.className = 'mode-item-icon';
    ic.textContent = m.icon || '';
    var tx = document.createElement('div'); tx.className = 'mode-item-text';
    var lb = document.createElement('div'); lb.className = 'mode-item-label';
    lb.textContent = m.label || m.id;
    var ds = document.createElement('div'); ds.className = 'mode-item-desc';
    ds.textContent = m.desc || '';
    tx.appendChild(lb); tx.appendChild(ds);
    it.appendChild(ic); it.appendChild(tx);
    if (m.id === currentMode) {
      var ck = document.createElement('span'); ck.className = 'mode-item-check'; ck.textContent = '✓';
      it.appendChild(ck);
    }
    modeMenu.appendChild(it);
  });
}
function toggleModeMenu() {
  if (!modeMenu) return;
  if (modeMenu.classList.contains('open')) { closeModeMenu(); return; }
  renderModeMenu();
  modeMenu.classList.add('open');
  if (modeBtn) modeBtn.setAttribute('aria-expanded', 'true');
}
function setMode(mode) {
  closeModeMenu();
  if (!mode || mode === currentMode) return Promise.resolve();
  var apply = function () { currentMode = mode; applyModeUI(mode); };
  if (!currentSid) { apply(); return Promise.resolve(); }
  return apiPost('/chat/' + encodeURIComponent(currentSid) + '/mode', { mode: mode })
    .then(function () { apply(); })
    .catch(function (e) { addBubble('切换模式失败: ' + ((e && e.message) || e), 'bot'); });
}
function loadModes() {
  return api('/modes').then(function (d) {
    MODES = d.modes || [];
    DEFAULT_MODE = d.default || 'chat';
    applyModeUI(currentMode);
  }).catch(function () { /* 拉不到模式列表就保持顶栏默认文案 */ });
}

if (modeBtn) modeBtn.addEventListener('click', function (e) { e.stopPropagation(); toggleModeMenu(); });
if (modeMenu) modeMenu.addEventListener('click', function (e) {
  var it = e.target.closest('.mode-item');
  if (!it) return;
  e.stopPropagation();
  setMode(it.getAttribute('data-mode'));
});
/* 点菜单外任意处收起下拉 */
document.addEventListener('click', function (e) {
  if (modeMenu && modeMenu.classList.contains('open')
      && !modeMenu.contains(e.target) && !(modeBtn && modeBtn.contains(e.target))) {
    closeModeMenu();
  }
});

function renderSessions(list, current) {
  var frag = document.createDocumentFragment();
  var arr = Object.keys(list).map(function (k) { return list[k]; })
    .sort(function (a, b) { return (b.created || 0) - (a.created || 0); });
  if (!arr.length) {
    var empty = document.createElement('div');
    empty.className = 'session-empty'; empty.textContent = '还没有对话';
    frag.appendChild(empty);
  }
  arr.forEach(function (s) {
    var d = document.createElement('div');
    d.className = 'session-item' + (s.id === current ? ' active' : '');
    d.dataset.sid = s.id;
    var t = document.createElement('span'); t.className = 't'; t.textContent = s.title || '新对话';
    var x = document.createElement('span'); x.className = 'del'; x.textContent = '✕';
    x.dataset.del = s.id; x.setAttribute('role', 'button'); x.setAttribute('aria-label', '删除对话');
    d.appendChild(t); d.appendChild(x);
    frag.appendChild(d);
  });
  sessionList.innerHTML = '';
  sessionList.appendChild(frag);
}

function segmentationOn() {
  return !!(SETTINGS && SETTINGS.appearance
    && SETTINGS.appearance.mobileMessageSegmentation === 'on');
}

function renderChat(msgs, title) {
  chatTitle.textContent = title || '新对话';
  var segOn = segmentationOn();
  var frag = document.createDocumentFragment();
  (msgs || []).forEach(function (m) {
    if (m.role === 'user') {
      var pu = makeBubble('user');
      pu.bub.textContent = m.content;
      frag.appendChild(pu.row);
      return;
    }
    /* assistant 走与 onSend 完全相同的分段路径。
       否则开了分段、刷新一次页面分段就消失，前后观感不一致。 */
    if (m.error) {
      /* 失败轮次：库里存的是 {content:"", error:"..."}，渲染成错误气泡，
         刷新后依然可重试，不会被当成正文显示 */
      frag.appendChild(makeErrorBubble(m.error).row);
      return;
    }
    var parts = segmentReply(m.content, segOn), i;
    for (i = 0; i < parts.length; i++) {
      var pb = makeBubble('bot', i > 0);
      mountMd(pb.bub, parts[i]);
      if (i === 0 && m.reasoning) pb.bub.insertBefore(makeReasoning(m.reasoning), pb.bub.firstChild);
      frag.appendChild(pb.row);
    }
    /* 历史里的提问卡一律定格成不可交互：这条早就答过了（或已经过期），
       再让用户点一次会重复发送同一条回答。只有当前 pendingAsk 才可交互。 */
    if (m.ask && m.ask.questions && m.ask.questions.length) {
      frag.appendChild(makeAskCard(m.ask, false).row);
    }
  });
  chat.innerHTML = '';
  chat.appendChild(frag);
  chat.scrollTop = chat.scrollHeight;
}

/* ================= 数据加载 ================= */
function loadSessions() {
  return api('/sessions').then(function (d) {
    renderSessions(d.sessions || {}, d.current);
    modelBadge.textContent = d.model || '';
    modelInfo.textContent = '模型: ' + (d.model || '—');
    if (!currentSid) { currentSid = d.current; return loadChat(); }
  });
}
function loadChat() {
  if (!currentSid) return Promise.resolve();
  return api('/chat/' + encodeURIComponent(currentSid)).then(function (d) {
    if (d.mode) { currentMode = d.mode; applyModeUI(d.mode); }
    renderChat(d.messages, d.title);
    renderTodos(d.todos);
    /* 待答提问：后端 session.pendingAsk 还在，说明用户刷新前没答。
       补一张可交互的卡片到末尾，让他接着答。
       renderChat 里那些历史卡是定格的，不会和这张重复响应点击。 */
    if (d.ask && d.ask.questions && d.ask.questions.length) {
      var stick = atBottom();
      chat.appendChild(makeAskCard(d.ask, true).row);
      scrollBottom(stick);
    }
  });
}
function newChat() {
  if (busy) return Promise.resolve();
  return apiPost('/chat/new', { mode: currentMode }).then(function (d) {
    currentSid = d.sid; setSidebar(false);
    if (d.mode) { currentMode = d.mode; applyModeUI(d.mode); }
    return Promise.all([loadSessions(), loadChat()]);
  }).catch(function (e) { addBubble('新建对话失败: ' + (e.message || e), 'bot'); });
}
function switchChat(sid) {
  if (busy || sid === currentSid) { setSidebar(false); return Promise.resolve(); }
  currentSid = sid; setSidebar(false);
  return Promise.all([loadSessions(), loadChat()])
    .catch(function (e) { addBubble('切换失败: ' + (e.message || e), 'bot'); });
}
function delChat(sid) {
  if (busy) return Promise.resolve();
  if (!confirm('删掉这个对话？')) return Promise.resolve();
  return api('/chat/' + encodeURIComponent(sid), { method: 'DELETE' }).then(function (d) {
    if (sid === currentSid) currentSid = (d && d.current) || null;
    return loadSessions().then(loadChat);
  }).catch(function (e) { addBubble('删除失败: ' + (e.message || e), 'bot'); });
}

sessionList.addEventListener('click', function (e) {
  var del = e.target.closest('[data-del]');
  if (del) { e.stopPropagation(); delChat(del.dataset.del); return; }
  var item = e.target.closest('.session-item');
  if (item) switchChat(item.dataset.sid);
});
$('new-chat-btn').addEventListener('click', newChat);

/* 代码块复制 */
chat.addEventListener('click', function (e) {
  var b = e.target.closest('.md-copy');
  if (!b) return;
  var code = CODE_STORE[b.dataset.cb];
  if (code == null) return;
  var done = function () {
    b.textContent = '已复制'; b.classList.add('done');
    setTimeout(function () { b.textContent = '复制'; b.classList.remove('done'); }, 1400);
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(code).then(done, function () { fallbackCopy(code, done); });
  } else fallbackCopy(code, done);
});
function fallbackCopy(text, done) {
  var ta = document.createElement('textarea');
  ta.value = text; ta.style.position = 'fixed'; ta.style.top = '-1000px'; ta.style.opacity = '0';
  document.body.appendChild(ta); ta.focus(); ta.select();
  try { document.execCommand('copy'); done(); } catch (e) { /* 忽略 */ }
  ta.remove();
}

/* ================= 发送 ================= */
function setBusy(on) {
  busy = on;
  setStatus(on ? 'thinking' : null);
  sendBtn.classList.toggle('cancel', on);
  sendBtn.textContent = on ? '■' : '➤';
  sendBtn.setAttribute('aria-label', on ? '停止等待' : '发送');
  sendBtn.disabled = false;
}

function onSend() {
  if (busy) {
    /* abort() 只断开浏览器到服务的连接；服务端 urlopen 无法被外部中断，
       那次调用会继续跑完。文案如实说明，不谎称「已停止生成」。 */
    if (aborter) { aborter.abort(); aborter = null; }
    return;
  }
  var text = input.value.trim();
  if (!text) return;
  input.value = ''; input.style.height = 'auto';

  addBubble(text, 'user');
  sendTurn(text, false);
}

/* 真正发一轮请求。isRetry=true 时后端不重复追加 user 消息（它还在库里），
   前端也不再画一个用户气泡。 */
function sendTurn(text, isRetry) {
  setBusy(true);
  showThinking();
  aborter = new AbortController();
  t0 = Date.now();

  api('/chat/' + encodeURIComponent(currentSid) + '/send', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message: text, retry: !!isRetry }), signal: aborter.signal
  }).then(function (r) {
    hideThinking();
    /* 失败轮次：错误串走 error 通道，渲染成错误气泡 + 重试按钮，
       不当正文显示，也已经被后端存成 error 标记而非 content */
    if (r && r.failed) {
      var stick = atBottom();
      var eb = makeErrorBubble(r.error);
      chat.appendChild(eb.row);
      scrollBottom(stick);
      setStatus('error');
      setTimeout(function () { if (!busy) setStatus(null); }, 2500);
    } else {
      addSegmented((r && r.response) || '(空回复)', r && r.reasoning);
    }
    if (r && r.tool_line) {
      var rows = chat.querySelectorAll('.msg-row.bot');
      var last = rows[rows.length - 1];
      if (last) {
        var bub = last.querySelector('.bubble');
        if (bub) {
          var t = document.createElement('div'); t.className = 'tool-info';
          t.textContent = '⚙️ ' + r.tool_line + '\n📎 ' + (r.tool_result || '');
          bub.appendChild(t);
        }
      }
    }
    if (r && !r.failed && r.usage && SETTINGS && SETTINGS.tts && SETTINGS.tts.autoSpeak) {
      speak(mdToPlain(r.response));
    }
    /* 阶段 2c：工作笔记进度条 + 结构化提问卡。
       todos 每轮都回传（update_todo 可能没被调用，此时沿用旧值），
       直接整表重渲染即可，不需要 diff。 */
    if (r && r.todos) renderTodos(r.todos);
    if (r && r.ask && r.ask.questions && r.ask.questions.length) {
      var stick2 = atBottom();
      chat.appendChild(makeAskCard(r.ask, true).row);
      scrollBottom(stick2);
    }
    api('/sessions').then(function (d) { renderSessions(d.sessions || {}, d.current); })
      .catch(function () { /* 侧边栏刷新失败不打断主流程 */ });
    if (settingsEl.classList.contains('open')) refreshUsagePanel();
  }).catch(function (e) {
    hideThinking();
    setStatus('error');
    var msg;
    if (e && e.name === 'AbortError') {
      msg = '（已取消等待，耗时 ' + Math.round((Date.now() - t0) / 1000)
        + 's。回复可能仍在后台生成，稍后刷新会话可看到。）';
    } else if (e && e.status === 409) {
      msg = '（上一条还在后台生成中，等它跑完再发吧。）';
    } else {
      msg = '连接失败: ' + ((e && e.message) || e);
    }
    addBubble(msg, 'bot');
    setTimeout(function () { if (!busy) setStatus(null); }, 2500);
  }).then(function () {
    setBusy(false); aborter = null;
    /* 不强制 focus()：几十秒后无用户手势弹键盘会导致视口乱跳 */
  });
}
sendBtn.addEventListener('click', onSend);

/* ================= TTS ================= */
var speakToken = 0;
function speak(text) {
  var t = String(text || '').trim();
  if (!t) return;
  var my = ++speakToken;
  /* termux-tts-speak 是阻塞式播放，长文本切成短句逐条送，避免一次卡很久 */
  var parts = t.split(/(?<=[。！？!?；;\n])/).map(function (s) { return s.trim(); })
    .filter(Boolean);
  var chain = Promise.resolve();
  parts.slice(0, 12).forEach(function (p) {
    chain = chain.then(function () {
      if (my !== speakToken) return;
      return apiPost('/tts', { text: p }).catch(function () { /* 播放失败静默 */ });
    });
  });
}
function stopSpeak() { speakToken++; apiPost('/tts/stop').catch(function () {}); }

/* ================= 设置抽屉 ================= */
var SETTINGS_TABS = [
  { key: 'appearance', label: '外观' },
  { key: 'model',      label: '模型' },
  { key: 'mode',       label: '模式' },
  { key: 'reasoning',  label: '思考' },
  { key: 'tools',      label: '工具' },
  { key: 'skills',     label: '技能' },
  { key: 'tts',        label: '语音' },
  { key: 'usage',      label: '用量' },
  { key: 'server',     label: '服务' }
];
var activeTab = 'appearance';
var dirty = false;

function openSettings(tab) {
  activeTab = tab || 'appearance';
  settingsEl.classList.add('open');
  settingsScrim.classList.add('on');
  renderSettingsNav();
  loadSettings();
}
function closeSettings() {
  if (dirty && !confirm('有未保存的修改，确定关闭？')) return;
  settingsEl.classList.remove('open');
  settingsScrim.classList.remove('on');
  dirty = false;
  setSaveStatus('');
}
/* 两个入口共用同一处理：侧边栏底部 ⚙ 与顶栏 ⚙ */
$('settings-btn').addEventListener('click', function () { openSettings(); });
$('top-settings-btn').addEventListener('click', function () { openSettings(); });
$('settings-close').addEventListener('click', closeSettings);
settingsScrim.addEventListener('click', closeSettings);

function renderSettingsNav() {
  settingsNav.innerHTML = '';
  SETTINGS_TABS.forEach(function (t) {
    var b = document.createElement('button');
    b.type = 'button'; b.className = 'nav-item' + (t.key === activeTab ? ' is-active' : '');
    b.textContent = t.label;
    b.addEventListener('click', function () {
      if (t.key === activeTab) return;
      activeTab = t.key; renderSettingsNav(); renderSettingsPanel();
      settingsBody.scrollTop = 0;
    });
    settingsNav.appendChild(b);
  });
}

function setSaveStatus(text, kind) {
  saveStatus.textContent = text || (dirty ? '有未保存的修改' : '已保存');
  saveStatus.parentNode.classList.remove('is-ok', 'is-err');
  if (kind === 'ok') saveStatus.parentNode.classList.add('is-ok');
  if (kind === 'err') saveStatus.parentNode.classList.add('is-err');
}
function markDirty() { dirty = true; setSaveStatus('有未保存的修改'); }

function loadSettings() {
  return api('/settings').then(function (d) {
    SETTINGS = d;
    applyAppearance(d.appearance || {});
    dirty = false; setSaveStatus('');
    renderSettingsPanel();
  }).catch(function (e) {
    settingsBody.innerHTML = '<div class="alert alert--err">设置读取失败: '
      + escHtml(e.message || e) + '</div>';
  });
}

/* 外观：主题 + 排版变量落到根节点（对应桌面端 applyMessageTypography） */
function applyAppearance(ap) {
  var theme = ap.theme === 'pearl-white' ? 'pearl-white' : 'charcoal-pink';
  document.documentElement.setAttribute('data-ui-theme', theme);
  var meta = document.querySelector('meta[name=theme-color]');
  if (meta) meta.setAttribute('content', theme === 'pearl-white' ? '#FFFFFF' : '#141414');
  var ty = ap.messageTypography || {};
  var root = document.documentElement.style;
  root.setProperty('--cy-msg-size', (ty.fontSize != null ? ty.fontSize : 15) + 'px');
  root.setProperty('--cy-msg-line-height', String(ty.lineHeight != null ? ty.lineHeight : 1.85));
  root.setProperty('--cy-msg-spacing', (ty.letterSpacing != null ? ty.letterSpacing : 0.8) + 'px');
  root.setProperty('--cy-msg-weight', String(ty.fontWeight != null ? ty.fontWeight : 400));
}

/* ---------- 控件工厂 ---------- */
function row(label, hint, controlEl, opts) {
  var o = opts || {};
  var r = document.createElement('div');
  r.className = 'cy-settings-row' + (o.stack ? ' cy-settings-row--stack' : '');
  var c = document.createElement('div'); c.className = 'cy-settings-row__copy';
  var st = document.createElement('strong'); st.textContent = label;
  c.appendChild(st);
  if (hint) { var sp = document.createElement('span'); sp.textContent = hint; c.appendChild(sp); }
  if (o.notice) {
    var nt = document.createElement('span'); nt.className = 'cy-settings-general__notice';
    nt.textContent = o.notice; c.appendChild(nt);
  }
  r.appendChild(c);
  if (controlEl) {
    var w = document.createElement('div'); w.className = 'cy-settings-row__control';
    w.appendChild(controlEl); r.appendChild(w);
  }
  return r;
}
function section(title, desc, cardEl) {
  var s = document.createElement('section'); s.className = 'cy-settings-section';
  if (title) {
    var h = document.createElement('div'); h.className = 'cy-settings-section__heading';
    var h2 = document.createElement('h2'); h2.textContent = title; h.appendChild(h2);
    if (desc) { var p = document.createElement('p'); p.textContent = desc; h.appendChild(p); }
    s.appendChild(h);
  }
  if (cardEl) s.appendChild(cardEl);
  return s;
}
function card() { var c = document.createElement('div'); c.className = 'card'; return c; }

function toggle(checked, onChange, label) {
  var l = document.createElement('label'); l.className = 'switch';
  var i = document.createElement('input');
  i.type = 'checkbox'; i.checked = !!checked;
  if (label) i.setAttribute('aria-label', label);
  var tr = document.createElement('span'); tr.className = 'switch__track';
  var th = document.createElement('span'); th.className = 'switch__thumb';
  tr.appendChild(th); l.appendChild(i); l.appendChild(tr);
  i.addEventListener('change', function () { onChange(i.checked); });
  return l;
}
function segmented(value, options, onChange) {
  var w = document.createElement('div'); w.className = 'segmented';
  /* current 必须可变并跟随点击更新。
     旧实现拿闭包里的初值 value 做守卫（if (o.value === value) return），
     初值永不改变，导致「点回原档位」被当成重复点击吞掉：
     初始 off → 点开启生效 → 再点关闭时 off === off 直接 return，切不回去。
     所有 segmented 控件（长回复分段 / 思考强度）都中招。 */
  var current = value;
  options.forEach(function (o) {
    var b = document.createElement('button');
    b.type = 'button'; b.textContent = o.label; b.disabled = !!o.disabled;
    if (o.value === current) b.classList.add('is-active');
    b.addEventListener('click', function () {
      if (b.disabled) return;
      if (o.value === current) return;
      current = o.value;
      Array.prototype.forEach.call(w.children, function (c) { c.classList.remove('is-active'); });
      b.classList.add('is-active');
      onChange(o.value);
    });
    w.appendChild(b);
  });
  return w;
}
function textInput(value, onChange, opts) {
  var o = opts || {};
  var i = document.createElement('input');
  i.className = 'form-input' + (o.mono ? ' form-input--mono' : '') + (o.num ? ' form-input--num' : '');
  i.type = o.password ? 'password' : (o.num ? 'number' : 'text');
  i.value = value == null ? '' : value;
  if (o.placeholder) i.placeholder = o.placeholder;
  if (o.min != null) i.min = o.min;
  if (o.max != null) i.max = o.max;
  if (o.step != null) i.step = o.step;
  if (o.label) i.setAttribute('aria-label', o.label);
  i.addEventListener('change', function () {
    onChange(o.num ? Number(i.value) : i.value);
  });
  return i;
}
function slider(min, max, step, value, onChange, fmt) {
  var w = document.createElement('div'); w.className = 'slider-row';
  var r = document.createElement('input');
  r.type = 'range'; r.min = min; r.max = max; r.step = step; r.value = value;
  var v = document.createElement('span'); v.className = 'slider-value';
  v.textContent = fmt ? fmt(value) : value;
  r.addEventListener('input', function () {
    var n = Number(r.value);
    v.textContent = fmt ? fmt(n) : n;
    onChange(n);
  });
  w.appendChild(r); w.appendChild(v);
  return w;
}
function btn(label, onClick, cls) {
  var b = document.createElement('button');
  b.type = 'button'; b.className = 'btn' + (cls ? ' ' + cls : '');
  b.textContent = label;
  b.addEventListener('click', onClick);
  return b;
}

/* ---------- 服务停止/重启后的全屏提示页 ---------- */
/* 服务关掉后前端与后端已断开，这层遮罩纯靠浏览器里已加载的 DOM 渲染，
   不发任何后端请求也能显示，所以 stop / restart 后都能看到它。 */
function showServiceDown(mode) {
  var old = document.getElementById('service-overlay');
  if (old) old.remove();
  var ov = document.createElement('div');
  ov.id = 'service-overlay';
  ov.className = 'service-overlay';
  var card = document.createElement('div');
  card.className = 'service-overlay__card';
  var ico = document.createElement('div');
  ico.className = 'service-overlay__icon';
  ico.textContent = mode === 'restart' ? '🔄' : '🌙';
  var h = document.createElement('h2');
  h.className = 'service-overlay__title';
  var p = document.createElement('p');
  p.className = 'service-overlay__text';
  card.appendChild(ico); card.appendChild(h); card.appendChild(p);
  ov.appendChild(card);
  document.body.appendChild(ov);

  function termuxHint() {
    h.textContent = '服务没有自动恢复';
    p.textContent = '守护器可能没在运行。请打开 Termux 手动执行：';
    var c = document.createElement('code');
    c.className = 'service-overlay__code';
    c.textContent = 'cyrene-web';
    card.appendChild(c);
  }

  if (mode === 'restart') {
    h.textContent = '服务重启中…';
    p.textContent = '正在重新连接，稍等几秒会自动恢复。';
    var hint = document.createElement('p');
    hint.className = 'service-overlay__hint';
    card.appendChild(hint);
    var tries = 0, maxTries = 15;   // 15 × 2s ≈ 30s 上限，别无限转
    var iv = setInterval(function () {
      tries++;
      fetch('/health', { cache: 'no-store' })
        .then(function (r) { if (!r.ok) throw new Error('bad status'); return r.json(); })
        .then(function () { clearInterval(iv); location.reload(); })
        .catch(function () {
          if (tries >= maxTries) { clearInterval(iv); hint.textContent = ''; termuxHint(); }
          else { hint.textContent = '仍在尝试重连… (' + tries + '/' + maxTries + ')'; }
        });
    }, 2000);
  } else {
    h.textContent = '服务已关闭';
    p.textContent = '网页已与后端断开。要重新开启，请打开 Termux 执行：';
    var code = document.createElement('code');
    code.className = 'service-overlay__code';
    code.textContent = 'cyrene-web';
    card.appendChild(code);
    var tip = document.createElement('p');
    tip.className = 'service-overlay__hint';
    tip.textContent = '这是彻底关闭，不会自动重启。';
    card.appendChild(tip);
  }
}

/* ---------- 保存 ---------- */
var saveTimer = null;
function saveSettings(patch, immediate) {
  var doSave = function () {
    return apiPost('/settings', patch).then(function (d) {
      if (d && d.settings) {
        SETTINGS = d.settings;
        applyAppearance(SETTINGS.appearance || {});
      }
      dirty = false;
      setSaveStatus(d && d.restart_required ? '已保存，重启服务后端口生效' : '已保存', 'ok');
      if (d && d.settings && d.settings.model) {
        modelBadge.textContent = d.settings.model.model || '';
        modelInfo.textContent = '模型: ' + (d.settings.model.model || '—');
      }
      return d;
    }).catch(function (e) {
      setSaveStatus('保存失败: ' + (e.message || e), 'err');
    });
  };
  markDirty();
  if (immediate) return doSave();
  if (saveTimer) clearTimeout(saveTimer);
  saveTimer = setTimeout(doSave, 420);
  return Promise.resolve();
}

/* ---------- 面板 ---------- */
function renderSettingsPanel() {
  settingsBody.innerHTML = '';
  if (!SETTINGS) return;
  var f = PANELS[activeTab];
  if (f) f(settingsBody, SETTINGS);
}

var PANELS = {};

PANELS.appearance = function (host, S) {
  var ap = S.appearance || {};
  var ty = ap.messageTypography || {};

  /* 主题 */
  var grid = document.createElement('div'); grid.className = 'theme-grid';
  [['charcoal-pink', '炭黑粉'], ['pearl-white', '珍珠白']].forEach(function (pair) {
    var b = document.createElement('button');
    b.type = 'button'; b.className = 'theme-card' + (ap.theme === pair[0] ? ' is-active' : '');
    b.dataset.theme = pair[0];
    b.setAttribute('aria-pressed', String(ap.theme === pair[0]));
    b.innerHTML = '<span class="theme-card__preview" aria-hidden="true"><i></i><i></i><i></i></span>'
      + '<span class="theme-card__label">' + pair[1]
      + '<span class="theme-card__tick" aria-hidden="true">✓</span></span>';
    b.addEventListener('click', function () {
      Array.prototype.forEach.call(grid.children, function (c) {
        c.classList.remove('is-active');
        c.setAttribute('aria-pressed', 'false');
      });
      b.classList.add('is-active');
      b.setAttribute('aria-pressed', 'true');
      saveSettings({ appearance: { theme: pair[0] } }, true);
    });
    grid.appendChild(b);
  });
  host.appendChild(section('界面主题', '色值取自桌面端同名主题文件', grid));

  /* 排版滑块，实时预览 */
  var ranges = {
    fontSize:      { min: 12,  max: 20,  step: 0.5,  fmt: function (v) { return v + 'px'; } },
    lineHeight:    { min: 1.2, max: 2.2, step: 0.05, fmt: function (v) { return v.toFixed(2); } },
    letterSpacing: { min: 0,   max: 2,   step: 0.1,  fmt: function (v) { return v.toFixed(1) + 'px'; } },
    fontWeight:    { min: 300, max: 700, step: 100,  fmt: function (v) { return String(v); } }
  };
  var labels = {
    fontSize: '字号', lineHeight: '行高',
    letterSpacing: '字间距', fontWeight: '字重'
  };
  var c1 = card();
  var pl = document.createElement('div'); pl.className = 'typo-preview__label';
  pl.textContent = '实时预览';
  var pv = document.createElement('div'); pv.className = 'typo-preview';
  pv.textContent = '人家在这里♪ 这行字会跟着滑块一起变。';
  Object.keys(ranges).forEach(function (k) {
    var rg = ranges[k];
    var cur = ty[k] != null ? ty[k] : (k === 'fontSize' ? 15 : k === 'lineHeight' ? 1.85
      : k === 'letterSpacing' ? 0.8 : 400);
    var patch = {};
    c1.appendChild(row(labels[k], null,
      slider(rg.min, rg.max, rg.step, cur, function (v) {
        patch[k] = v;
        /* 先本地预览，再落盘 */
        var root = document.documentElement.style;
        if (k === 'fontSize') root.setProperty('--cy-msg-size', v + 'px');
        if (k === 'lineHeight') root.setProperty('--cy-msg-line-height', String(v));
        if (k === 'letterSpacing') root.setProperty('--cy-msg-spacing', v + 'px');
        if (k === 'fontWeight') root.setProperty('--cy-msg-weight', String(v));
        ty[k] = v;
        saveSettings({ appearance: { messageTypography: patch } });
      }, rg.fmt), { stack: true }));
  });
  var sec2 = section('回复排版', '只作用于昔涟回复气泡的正文，与你发的消息无关', c1);
  sec2.insertBefore(pl, c1);
  sec2.appendChild(pv);
  host.appendChild(sec2);

  /* 消息分段 */
  var c2 = card();
  c2.appendChild(row('长回复分段', '把一段长回复按语义拆成多个气泡，手机上更好读',
    segmented(ap.mobileMessageSegmentation || 'off',
      [{ label: '关闭', value: 'off' }, { label: '开启', value: 'on' }],
      function (v) {
        saveSettings({ appearance: { mobileMessageSegmentation: v } }, true);
      }), null));
  host.appendChild(section('消息展示', null, c2));

  host.appendChild(btn('恢复默认排版', function () {
    saveSettings({ appearance: { messageTypography: {
      fontSize: 15, lineHeight: 1.85, letterSpacing: 0.8, fontWeight: 400
    } } }, true).then(function () { renderSettingsPanel(); });
  }, 'btn--ghost'));
  host.lastChild.style.margin = '12px 14px 0';
};

PANELS.model = function (host, S) {
  var m = S.model || {};
  var c = card();

  c.appendChild(row('接口地址', 'OpenAI 兼容格式的 Base URL',
    textInput(m.api_base, function (v) { saveSettings({ model: { api_base: v } }); },
      { mono: true, label: '接口地址' }), { stack: true }));

  /* API Key 只写不回显：服务端只给 api_key_set 布尔 */
  var wrap = document.createElement('div'); wrap.className = 'reveal-wrap';
  var ki = document.createElement('input');
  ki.className = 'form-input form-input--mono'; ki.type = 'password';
  ki.placeholder = m.api_key_set ? '已配置（留空则不修改）' : '尚未配置';
  ki.setAttribute('aria-label', 'API Key');
  var showBtn = btn('显示', function () {
    ki.type = ki.type === 'password' ? 'text' : 'password';
    showBtn.textContent = ki.type === 'password' ? '显示' : '隐藏';
  }, 'btn--sm');
  wrap.appendChild(ki); wrap.appendChild(showBtn);
  var keyRow = row('API Key', '出于安全考虑不会回显已保存的值', wrap, { stack: true });
  c.appendChild(keyRow);
  ki.addEventListener('change', function () {
    if (ki.value.trim()) saveSettings({ model: { api_key: ki.value.trim() } }, true)
      .then(function () { ki.value = ''; ki.placeholder = '已配置（留空则不修改）'; });
  });

  c.appendChild(row('模型名称', null,
    textInput(m.model, function (v) { saveSettings({ model: { model: v } }); },
      { mono: true, label: '模型名称' }), { stack: true }));

  /* 采样参数：对齐桌面端 customStyle.diversity 的 driver/value 语义，
     这里直接暴露底层参数，因为手机端只有一个模型通道 */
  var c2 = card();
  c2.appendChild(row('Temperature', '越高越发散。桌面端默认 0.65',
    slider(0, 2, 0.01, m.temperature != null ? m.temperature : 0.7,
      function (v) { saveSettings({ model: { temperature: v } }); },
      function (v) { return v.toFixed(2); }), { stack: true }));
  c2.appendChild(row('Top-P', '核采样。留 1 表示不裁剪',
    slider(0.1, 1, 0.01, m.top_p != null ? m.top_p : 1,
      function (v) { saveSettings({ model: { top_p: v } }); },
      function (v) { return v.toFixed(2); }), { stack: true }));
  c2.appendChild(row('重复惩罚', 'frequency_penalty，越高越抑制重复用词',
    slider(-2, 2, 0.05, m.frequency_penalty != null ? m.frequency_penalty : 0,
      function (v) { saveSettings({ model: { frequency_penalty: v } }); },
      function (v) { return v.toFixed(2); }), { stack: true }));
  c2.appendChild(row('新话题倾向', 'presence_penalty，越高越爱引入新话题',
    slider(-2, 2, 0.05, m.presence_penalty != null ? m.presence_penalty : 0,
      function (v) { saveSettings({ model: { presence_penalty: v } }); },
      function (v) { return v.toFixed(2); }), { stack: true }));
  c2.appendChild(row('单次最大输出', 'max_tokens，工具回填那次固定 500',
    slider(256, 8192, 128, m.max_tokens != null ? m.max_tokens : 2000,
      function (v) { saveSettings({ model: { max_tokens: v } }); },
      function (v) { return String(v); }), { stack: true }));
  c2.appendChild(row('请求超时', '秒。到点直接报错，不再无限等',
    slider(15, 300, 5, m.request_timeout != null ? m.request_timeout : 120,
      function (v) { saveSettings({ model: { request_timeout: v } }); },
      function (v) { return v + 's'; }), { stack: true }));
  c2.appendChild(row('携带历史上限', '最多带多少轮进上下文，超出从头部丢弃',
    slider(4, 100, 2, m.max_history != null ? m.max_history : 30,
      function (v) { saveSettings({ model: { max_history: v } }); },
      function (v) { return v + ' 轮'; }), { stack: true }));

  host.appendChild(section('模型接入', '改动立即写入 .config.json，下一次请求生效', c));
  host.appendChild(section('生成参数', null, c2));
  host.appendChild(btn('恢复生成参数默认值', function () {
    saveSettings({ model: { temperature: 0.7, top_p: 1, frequency_penalty: 0,
      presence_penalty: 0, max_tokens: 2000, request_timeout: 120, max_history: 30 } }, true)
      .then(function () { renderSettingsPanel(); });
  }, 'btn--ghost'));
  host.lastChild.style.margin = '12px 14px 0';
};

PANELS.tools = function (host, S) {
  var note = document.createElement('div');
  note.className = 'alert alert--warn';
  note.textContent = '关掉某个工具后，昔涟的系统提示里就不再出现它，'
    + '服务端也会拒绝执行——不是只藏起来，是真的调不动。';
  host.appendChild(note);

  var search = document.createElement('div'); search.className = 'tool-panel__toolbar';
  var si = document.createElement('input');
  si.className = 'tool-panel__search'; si.placeholder = '搜索工具…';
  si.setAttribute('aria-label', '搜索工具');
  search.appendChild(si);
  host.appendChild(search);

  var grid = document.createElement('div'); grid.className = 'tool-panel__grid';
  var countEl = document.createElement('div'); countEl.className = 'tool-panel__count';
  host.appendChild(countEl);
  host.appendChild(grid);

  function draw(filter) {
    api('/tools').then(function (d) {
      var tools = d.tools || [];
      var kw = String(filter || '').trim().toLowerCase();
      var shown = kw ? tools.filter(function (t) {
        return (t.id + ' ' + t.desc).toLowerCase().indexOf(kw) >= 0;
      }) : tools;
      shown.sort(function (a, b) {
        if (!!a.enabled !== !!b.enabled) return a.enabled ? -1 : 1;
        return a.id.localeCompare(b.id);
      });
      grid.innerHTML = '';
      countEl.textContent = '已启用 ' + tools.filter(function (t) { return t.enabled; }).length
        + ' / ' + tools.length;
      if (!shown.length) {
        var e = document.createElement('div'); e.className = 'tool-panel__empty';
        e.textContent = '没有匹配的工具'; grid.appendChild(e); return;
      }
      shown.forEach(function (t) {
        var el = document.createElement('div');
        el.className = 'tool-card' + (t.enabled ? '' : ' is-off');
        var ic = document.createElement('span'); ic.className = 'tool-card__icon';
        ic.textContent = t.icon || '⚙';
        var bd = document.createElement('div'); bd.className = 'tool-card__body';
        var nm = document.createElement('div'); nm.className = 'tool-card__name';
        nm.textContent = t.desc || t.id;
        var idsp = document.createElement('span'); idsp.className = 'tool-card__id';
        idsp.textContent = t.id; nm.appendChild(idsp);
        var ds = document.createElement('div'); ds.className = 'tool-card__desc';
        ds.textContent = t.cmd_preview || '';
        bd.appendChild(nm); bd.appendChild(ds);
        var pill = document.createElement('button');
        pill.type = 'button'; pill.className = 'tool-card__pill' + (t.enabled ? ' is-on' : '');
        pill.setAttribute('role', 'switch'); pill.setAttribute('aria-checked', String(!!t.enabled));
        pill.setAttribute('aria-label', (t.enabled ? '关闭 ' : '开启 ') + t.id);
        var knob = document.createElement('span'); knob.className = 'tool-card__pill-knob';
        pill.appendChild(knob);
        pill.addEventListener('click', function () {
          apiPost('/tools/' + encodeURIComponent(t.id), { enabled: !t.enabled })
            .then(function () { draw(si.value); })
            .catch(function (e) { alert('切换失败: ' + (e.message || e)); });
        });
        el.appendChild(ic); el.appendChild(bd); el.appendChild(pill);
        grid.appendChild(el);
      });
    }).catch(function (e) {
      grid.innerHTML = '<div class="alert alert--err">工具列表读取失败: '
        + escHtml(e.message || e) + '</div>';
    });
  }
  si.addEventListener('input', function () { draw(si.value); });
  draw('');

  var actions = document.createElement('div');
  actions.style.cssText = 'display:flex;gap:8px;padding:12px 14px 0';
  actions.appendChild(btn('全部开启', function () {
    apiPost('/tools/bulk', { enabled: true }).then(function () { draw(si.value); })
      .catch(function (e) { alert('操作失败: ' + (e.message || e)); });
  }, 'btn--ghost btn--sm'));
  /* 只读批量：从 /tools 动态取 readonly=true 的 id，不再硬编码清单。
     旧实现写死 4 个硬件只读工具，2b 加的 web_search/fetch_url（也是
     readonly=true）与 2c 加的 read_file/glob_files/invoke_skill 等一律漏掉，
     按钮语义变成「只留 4 个硬件工具」而非它字面写的「只留只读工具」。
     动态取之后新增只读工具自动纳入，不需要再来改前端。 */
  actions.appendChild(btn('只留只读工具', function () {
    api('/tools').then(function (d) {
      var only = (d.tools || []).filter(function (t) { return t.readonly; })
        .map(function (t) { return t.id; });
      if (!only.length) { alert('当前没有标记为只读的工具'); return; }
      return apiPost('/tools/bulk', { enabled: false }).then(function () {
        return apiPost('/tools/bulk', { enabled: true, only: only });
      }).then(function () { draw(si.value); });
    }).catch(function (e) { alert('操作失败: ' + (e.message || e)); });
  }, 'btn--ghost btn--sm'));
  host.appendChild(actions);
};

PANELS.skills = function (host, S) {
  var note = document.createElement('div');
  note.className = 'alert alert--info';
  note.textContent = '技能来自 ~/cyrene/skills/*/SKILL.md。开启后其正文会注入系统提示，'
    + '关掉则完全不注入——直接影响昔涟的行为策略。';
  host.appendChild(note);

  var grid = document.createElement('div'); grid.className = 'tool-panel__grid';
  host.appendChild(grid);

  api('/skills').then(function (d) {
    var skills = d.skills || [];
    if (!skills.length) {
      grid.innerHTML = '<div class="tool-panel__empty">没有发现技能目录</div>';
      return;
    }
    skills.forEach(function (s) {
      var el = document.createElement('div');
      el.className = 'tool-card' + (s.enabled ? '' : ' is-off');
      var ic = document.createElement('span'); ic.className = 'tool-card__icon';
      ic.textContent = '✦';
      var bd = document.createElement('div'); bd.className = 'tool-card__body';
      var nm = document.createElement('div'); nm.className = 'tool-card__name';
      nm.textContent = s.name || s.id;
      if (s.version) {
        var vb = document.createElement('span'); vb.className = 'tool-card__badge';
        vb.textContent = 'v' + s.version; nm.appendChild(vb);
      }
      if (s.autoInject) {
        var ab = document.createElement('span'); ab.className = 'tool-card__badge';
        ab.textContent = '自动注入'; nm.appendChild(ab);
      }
      var ds = document.createElement('div'); ds.className = 'tool-card__desc';
      ds.textContent = s.description || '';
      ds.style.whiteSpace = 'normal';
      bd.appendChild(nm); bd.appendChild(ds);
      var pill = document.createElement('button');
      pill.type = 'button'; pill.className = 'tool-card__pill' + (s.enabled ? ' is-on' : '');
      pill.setAttribute('role', 'switch'); pill.setAttribute('aria-checked', String(!!s.enabled));
      pill.setAttribute('aria-label', (s.enabled ? '关闭技能 ' : '开启技能 ') + s.id);
      var knob = document.createElement('span'); knob.className = 'tool-card__pill-knob';
      pill.appendChild(knob);
      pill.addEventListener('click', function () {
        apiPost('/skills/' + encodeURIComponent(s.id), { enabled: !s.enabled })
          .then(function () { renderSettingsPanel(); })
          .catch(function (e) { alert('切换失败: ' + (e.message || e)); });
      });
      el.appendChild(ic); el.appendChild(bd); el.appendChild(pill);
      grid.appendChild(el);
    });
  }).catch(function (e) {
    grid.innerHTML = '<div class="alert alert--err">技能列表读取失败: '
      + escHtml(e.message || e) + '</div>';
  });
};

PANELS.tts = function (host, S) {
  var t = S.tts || {};
  var c = card();
  c.appendChild(row('自动朗读回复', '昔涟回完话就用系统 TTS 念出来',
    toggle(t.autoSpeak, function (v) { saveSettings({ tts: { autoSpeak: v } }, true); },
      '自动朗读回复'), null));
  c.appendChild(row('语速', 'termux-tts-speak -r，1.0 为正常',
    slider(0.5, 2.0, 0.1, t.rate != null ? t.rate : 1.0,
      function (v) { saveSettings({ tts: { rate: v } }); },
      function (v) { return v.toFixed(1) + '×'; }), { stack: true }));
  c.appendChild(row('音调', 'termux-tts-speak -p，1.0 为正常',
    slider(0.5, 2.0, 0.1, t.pitch != null ? t.pitch : 1.0,
      function (v) { saveSettings({ tts: { pitch: v } }); },
      function (v) { return v.toFixed(1); }), { stack: true }));
  c.appendChild(row('语言', '留空则用系统默认',
    textInput(t.language || '', function (v) { saveSettings({ tts: { language: v } }); },
      { placeholder: 'zh-CN', label: '语言' }), null));

  host.appendChild(section('语音朗读', '依赖 Termux:API 的 termux-tts-speak；'
    + '电脑上没有这个命令，播放会静默失败', c));

  var c2 = card();
  var testRow = row('试听', null, null);
  var ti = document.createElement('input');
  ti.className = 'form-input'; ti.value = '嗨♪ 人家的声音是这样子的。';
  ti.setAttribute('aria-label', '试听文本');
  var acts = document.createElement('div');
  acts.style.cssText = 'display:flex;gap:8px;margin-top:9px';
  acts.appendChild(btn('播放', function () { speak(ti.value); }, 'btn--primary btn--sm'));
  acts.appendChild(btn('停止', function () { stopSpeak(); }, 'btn--ghost btn--sm'));
  var wrap = document.createElement('div'); wrap.appendChild(ti); wrap.appendChild(acts);
  testRow.appendChild(wrap);
  c2.appendChild(testRow);
  host.appendChild(section(null, null, c2));
};

var usageCache = null;
function refreshUsagePanel() {
  if (activeTab !== 'usage') return;
  api('/usage').then(function (d) {
    usageCache = d;
    renderSettingsPanel();
  }).catch(function () { /* 用量读取失败不阻断 */ });
}
PANELS.usage = function (host, S) {
  var u = usageCache || S.usage || {};
  var grid = document.createElement('div'); grid.className = 'usage-grid';
  function cell(label, value, unit) {
    var c = document.createElement('div'); c.className = 'usage-cell';
    var l = document.createElement('div'); l.className = 'usage-cell__label'; l.textContent = label;
    var v = document.createElement('div'); v.className = 'usage-cell__value';
    v.textContent = value;
    if (unit) { var s = document.createElement('span'); s.className = 'usage-cell__unit';
      s.textContent = unit; v.appendChild(s); }
    c.appendChild(l); c.appendChild(v);
    return c;
  }
  function fmtNum(n) {
    n = Number(n) || 0;
    if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M';
    if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
    return String(n);
  }
  grid.appendChild(cell('请求次数', fmtNum(u.requests), '次'));
  grid.appendChild(cell('总 Token', fmtNum(u.totalTokens)));
  grid.appendChild(cell('输入 Token', fmtNum(u.promptTokens)));
  grid.appendChild(cell('输出 Token', fmtNum(u.completionTokens)));
  grid.appendChild(cell('工具调用', fmtNum(u.toolCalls), '次'));
  grid.appendChild(cell('会话数', fmtNum(u.sessions), '个'));
  host.appendChild(section('用量统计', '从本次服务启动开始累计，重启后归零', grid));

  if (u.totalTokens) {
    var bar = document.createElement('div'); bar.style.padding = '10px 14px 0';
    var pct = Math.min(100, Math.round((u.completionTokens || 0) / (u.totalTokens || 1) * 100));
    bar.innerHTML = '<div class="usage-bar"><div class="usage-bar__fill" style="width:'
      + pct + '%"></div></div>';
    var cap = document.createElement('div');
    cap.className = 'usage-note'; cap.style.padding = '5px 0 0';
    cap.textContent = '输出占比 ' + pct + '%';
    bar.appendChild(cap);
    host.appendChild(bar);
  }

  if (u.byModel && Object.keys(u.byModel).length) {
    var c = card();
    Object.keys(u.byModel).forEach(function (mk) {
      var mv = u.byModel[mk] || {};
      c.appendChild(row(mk, (mv.requests || 0) + ' 次请求 · '
        + fmtNum(mv.totalTokens || 0) + ' token', null));
    });
    host.appendChild(section('按模型', null, c));
  }

  var note = document.createElement('div');
  note.className = 'usage-note';
  note.textContent = u.lastError ? ('最近一次错误: ' + u.lastError) : '暂无错误记录';
  host.appendChild(note);

  var acts = document.createElement('div');
  acts.style.cssText = 'display:flex;gap:8px;padding:12px 14px 0';
  acts.appendChild(btn('清零统计', function () {
    if (!confirm('清空用量统计？')) return;
    apiPost('/usage/reset').then(function () { usageCache = null; renderSettingsPanel(); });
  }, 'btn--ghost btn--sm'));
  acts.appendChild(btn('刷新', function () { refreshUsagePanel(); }, 'btn--ghost btn--sm'));
  host.appendChild(acts);
};

PANELS.server = function (host, S) {
  var sv = S.server || {};
  var c = card();
  c.appendChild(row('监听端口', '改完需要重启服务才生效',
    textInput(sv.web_port, function (v) { saveSettings({ server: { web_port: v } }); },
      { num: true, min: 1, max: 65535, label: '监听端口' }), { notice: '需要重启服务' }));
  c.appendChild(row('监听地址', '0.0.0.0 = 局域网可访问；127.0.0.1 = 仅本机',
    textInput(sv.bind_host, function (v) { saveSettings({ server: { bind_host: v } }); },
      { mono: true, label: '监听地址' }), { notice: '需要重启服务' }));
  host.appendChild(section('服务', null, c));

  if (String(sv.bind_host) === '0.0.0.0') {
    var w = document.createElement('div'); w.className = 'alert alert--warn';
    w.textContent = '当前对局域网开放，而 shell 工具可在手机上执行任意命令。'
      + '同一 WiFi 下的其他设备拿到地址就能操作你的手机。'
      + '不需要外部访问时建议改成 127.0.0.1。';
    host.appendChild(w);
  }

  var c2 = card();
  c2.appendChild(row('工具执行超时', '秒。termux-* 命令卡住时的兜底',
    slider(5, 120, 5, sv.tool_timeout != null ? sv.tool_timeout : 30,
      function (v) { saveSettings({ server: { tool_timeout: v } }); },
      function (v) { return v + 's'; }), { stack: true }));
  c2.appendChild(row('Markdown 渲染', '关掉则回复按纯文本显示',
    toggle(S.appearance && S.appearance.markdown !== false,
      function (v) { saveSettings({ appearance: { markdown: v } }, true); },
      'Markdown 渲染'), null));
  c2.appendChild(row('代码高亮', '关掉可省一点手机性能',
    toggle(S.appearance && S.appearance.highlight !== false,
      function (v) { saveSettings({ appearance: { highlight: v } }, true); },
      '代码高亮'), null));
  host.appendChild(section('运行时', null, c2));

  var c3 = card();
  var info = row('版本', 'cyrene_web.py v8 · marked v15.0.12 · highlight.js v11.11.1', null);
  c3.appendChild(info);
  c3.appendChild(row('数据目录', String(S.info && S.info.data_dir || '~/cyrene/data'), null));
  c3.appendChild(row('技能目录', String(S.info && S.info.skills_dir || '~/cyrene/skills'), null));
  c3.appendChild(row('会话数', String((S.info && S.info.session_count) || 0) + ' 个', null));
  host.appendChild(section('关于', null, c3));

  var acts = document.createElement('div');
  acts.style.cssText = 'display:flex;gap:8px;padding:12px 14px 0';
  acts.appendChild(btn('清空所有会话', function () {
    if (!confirm('删掉全部对话记录？这一步不可撤销。')) return;
    apiPost('/sessions/clear').then(function () {
      currentSid = null;
      return loadSessions();
    }).then(function () { renderSettingsPanel(); })
      .catch(function (e) { alert('失败: ' + (e.message || e)); });
  }, 'btn--danger btn--sm'));
  acts.appendChild(btn('导出会话 JSON', function () {
    api('/sessions/export').then(function (d) {
      var blob = new Blob([JSON.stringify(d, null, 2)], { type: 'application/json' });
      var a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'cyrene-sessions-' + Date.now() + '.json';
      a.click();
      setTimeout(function () { URL.revokeObjectURL(a.href); }, 4000);
    }).catch(function (e) { alert('导出失败: ' + (e.message || e)); });
  }, 'btn--ghost btn--sm'));
  host.appendChild(acts);

  /* ---------- 服务控制：重启 / 结束 ---------- */
  var svcSec = card();
  svcSec.appendChild(row('重启服务', '关掉当前进程，守护器约 3 秒后拉起新实例。改过端口/配置想让它生效就用这个', null,
    { notice: '网页会短暂断开后自动恢复' }));
  svcSec.appendChild(row('结束服务', '彻底停止，并让守护器不再自动重启', null,
    { notice: '网页会断开，需去 Termux 敲 cyrene-web 才能重开' }));
  var svcActs = document.createElement('div');
  svcActs.style.cssText = 'display:flex;gap:8px;padding:12px 14px 0';
  svcActs.appendChild(btn('重启服务', function () {
    if (!confirm('重启服务？网页会短暂断开后自动恢复。')) return;
    showServiceDown('restart');   // 先铺遮罩，再发请求；断连即预期
    apiPost('/service/restart', { mode: 'restart' })
      .catch(function () { /* 关闭导致的断连属正常，遮罩已在轮询重连 */ });
  }, 'btn--ghost btn--sm'));
  svcActs.appendChild(btn('结束服务', function () {
    if (!confirm('彻底关闭服务？网页会断开，需去 Termux 执行 cyrene-web 才能重开。')) return;
    showServiceDown('stop');
    apiPost('/service/stop', { mode: 'stop' })
      .catch(function () { /* 同上，断连即预期 */ });
  }, 'btn--danger btn--sm'));
  host.appendChild(section('服务控制', null, svcSec));
  host.appendChild(svcActs);
};

/* ---------- 模式：默认模式 + 各模式提示词规模 ---------- */
PANELS.mode = function (host, S) {
  var chat = S.chat || {};
  var info = S.info || {};
  var byMode = info.prompt_chars_by_mode || {};
  var cur = chat.defaultMode || 'chat';
  var list = (MODES && MODES.length) ? MODES : [
    { id: 'chat', label: '聊天', icon: '💬', tools: false },
    { id: 'work', label: '工作', icon: '🛠️', tools: true },
    { id: 'code', label: '代码', icon: '💻', tools: true },
    { id: 'learn', label: '学习', icon: '📚', tools: true }
  ];

  /* 默认模式卡片列表，选中项高亮 */
  var grid = document.createElement('div'); grid.className = 'mode-grid';
  list.forEach(function (m) {
    var active = m.id === cur;
    var b = document.createElement('button');
    b.type = 'button';
    b.className = 'mode-card' + (active ? ' is-active' : '');
    b.setAttribute('aria-pressed', String(active));
    b.innerHTML =
      '<span class="mode-card__icon" aria-hidden="true">' + (m.icon || '💬') + '</span>'
      + '<span class="mode-card__label">' + escHtml(m.label || m.id)
      + '<span class="mode-card__tick" aria-hidden="true">✓</span></span>'
      + '<span class="mode-card__desc">' + escHtml(m.desc || '') + '</span>'
      + '<span class="mode-card__meta">'
      + (m.tools ? '可用工具' : '无工具')
      + (byMode[m.id] ? ' · ' + Number(byMode[m.id]).toLocaleString('en-US') + ' 字' : '')
      + '</span>';
    b.addEventListener('click', function () {
      if (m.id === cur) return;
      cur = m.id;
      Array.prototype.forEach.call(grid.children, function (c) {
        c.classList.remove('is-active');
        c.setAttribute('aria-pressed', 'false');
      });
      b.classList.add('is-active');
      b.setAttribute('aria-pressed', 'true');
      saveSettings({ chat: { defaultMode: m.id } }, true);
    });
    grid.appendChild(b);
  });
  host.appendChild(section('默认模式', '新建对话时使用的模式；已有对话各自的模式不受影响，'
    + '可以在顶栏单独切换', grid));

  /* 提示词规模对照 */
  var c = card();
  var total = 0;
  list.forEach(function (m) {
    var n = Number(byMode[m.id]) || 0;
    total += n;
    c.appendChild(row((m.icon || '') + ' ' + (m.label || m.id),
      n ? n.toLocaleString('en-US') + ' 字符' : '未加载', null));
  });
  host.appendChild(section('系统提示词规模', '四个模式共用 soul.md 与语气基准，'
    + '差异来自各自的 identity / system / remark 三件套和工具块'
    + (total ? '；合计 ' + total.toLocaleString('en-US') + ' 字符' : ''), c));

  var c2 = card();
  c2.appendChild(row('当前对话模式', currentMode ? (currentMode + '（顶栏可切换）') : '—', null));
  c2.appendChild(row('chat 模式工具拦截', '双层：提示词不注入工具块 + Runtime 剥掉 [TOOL] 行', null));
  host.appendChild(section('说明', null, c2));
};

/* ---------- 思考：reasoning 总开关 + effort 三档 + showInChat ---------- */
PANELS.reasoning = function (host, S) {
  var rs = S.reasoning || {};
  var enabled = !!rs.enabled;
  var effort = rs.effort || 'medium';
  var showInChat = rs.showInChat !== false;

  var c = card();
  /* S 就是全局 SETTINGS 的引用：先改本地镜像再重绘，否则 saveSettings 的
     Promise 还没回来，renderSettingsPanel 读到旧值，开关会当场弹回去 */
  function patchReasoning(key, value, redraw) {
    S.reasoning = S.reasoning || {};
    S.reasoning[key] = value;
    var p = {}; p[key] = value;
    saveSettings({ reasoning: p }, true);
    if (redraw) renderSettingsPanel();
  }

  c.appendChild(row('开启思考链', '让模型先推理再作答，回复更稳但更慢、更耗 token',
    toggle(enabled, function (v) {
      patchReasoning('enabled', v, true);
    }, '开启思考链'), { notice: '改完立即生效' }));

  c.appendChild(row('思考强度', 'low 快而省，high 想得深也慢；部分模型不支持会被自动忽略',
    segmented(effort, [
      { value: 'low', label: '轻', disabled: !enabled },
      { value: 'medium', label: '中', disabled: !enabled },
      { value: 'high', label: '深', disabled: !enabled }
    ], function (v) { patchReasoning('effort', v, false); }), null));

  c.appendChild(row('在对话里显示思考过程', '关掉则只在后台记录，气泡上方不出现折叠块',
    toggle(showInChat, function (v) {
      patchReasoning('showInChat', v, false);
    }, '在对话里显示思考过程'), null));

  host.appendChild(section('思考链', '同时注入 reasoning_effort / reasoning.effort / enable_thinking '
    + '三种字段兼容不同厂商；返回 400/422 会自动剥掉这些参数重试一次', c));

  if (!enabled) {
    var w = document.createElement('div'); w.className = 'alert alert--warn';
    w.textContent = '思考链当前关闭，下面的强度档位不生效。';
    host.appendChild(w);
  }

  /* 当前会话是否已有思考内容，帮助用户确认效果 */
  var c2 = card();
  var u = S.usage || {};
  c2.appendChild(row('最近一次错误', u.lastError ? String(u.lastError) : '暂无', null));
  c2.appendChild(row('思考块渲染', '默认收起，点标题展开；正文与思考走同一条 sanitize 净化路径', null));
  host.appendChild(section('状态', null, c2));
};

$('settings-save').addEventListener('click', function () {
  saveSettings({}, true);
});

/* ================= 启动 ================= */
if (window.visualViewport) {
  window.visualViewport.addEventListener('resize', function () { scrollBottom(false); });
}
window.addEventListener('resize', function () { if (!isMobile()) setSidebar(true); });
document.addEventListener('keydown', function (e) {
  if (e.key === 'Escape' && settingsEl.classList.contains('open')) closeSettings();
});

setSidebar(!isMobile());
/* 先取模式列表 + 设置，再载会话，保证首屏主题/排版/模式图标都正确 */
Promise.all([
  loadModes(),
  api('/settings').then(function (d) {
    SETTINGS = d;
    applyAppearance(d.appearance || {});
  }).catch(function () { /* 设置拉取失败不阻断启动 */ })
]).then(function () {
  return loadSessions();
}).catch(function (e) {
  addBubble('初始化失败: ' + ((e && e.message) || e), 'bot');
});

/* 暴露给调试用，不参与正常流程 */
window.__cyrene = {
  get settings() { return SETTINGS; },
  openSettings: openSettings, speak: speak, stopSpeak: stopSpeak
};
})();

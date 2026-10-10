/* ============================================================
   昔涟 · 手机版 Web 前端主逻辑
   分两块：聊天核心 / 设置抽屉
   安全不变量：Markdown 渲染不透传原始 HTML，渲染后再过一遍 sanitize
   ============================================================ */
(function () {
'use strict';

var $ = function (id) { return document.getElementById(id); };

/* ================= 图标 =================
   图标全部来自 index 里内联的 sprite（<symbol id="ic-*">），来源是
   lucide-static 与桌面端自绘 SVG。sprite 的 stroke 已是 currentColor，
   所以颜色只由 CSS 的 color 决定 —— 主题切换、hover、禁用态都不用另做。

   用 DOM 构造而不是 innerHTML：这些图标名有一部分来自后端数据
   （/tools、/modes 的 icon 字段），拼字符串会开一个注入面。
   全部走 createElementNS + setAttribute，名字只作 attribute 值使用。 */
var SVG_NS = 'http://www.w3.org/2000/svg';
var XLINK_NS = 'http://www.w3.org/1999/xlink';

/* 后端 icon 取值 → sprite symbol 名。
   未命中时 iconFor 返回 null，调用方回退为原样文本。 */
var ICON_MAP = {
  /* 模式 */
  chat: 'chat', work: 'work', code: 'code', learn: 'learn',
  /* 工具：设备 */
  battery: 'battery', camera: 'camera', tts: 'volume', notify: 'bell',
  vibrate: 'vibrate', torch: 'flashlight', location: 'map-pin',
  clipboard_set: 'clipboard-copy', clipboard_get: 'clipboard',
  wifi_info: 'wifi', brightness: 'sun',
  /* 工具：命令与文件 */
  run_shell: 'terminal', shell_job: 'satellite',
  read_file: 'book-text', write_file: 'file-pen', edit_file: 'scissors',
  glob_files: 'search', grep_files: 'search', list_dir: 'folder',
  /* 工具：联网 */
  web_search: 'globe', fetch_url: 'doc', download_file: 'download',
  /* 工具：交互 */
  update_todo: 'list-todo', ask_user: 'help', invoke_skill: 'sparkles',
  read_skill_reference: 'clip', read_tool_result: 'history',
  /* 插件风险等级 */
  risk_safe: 'check', risk_low: 'info', risk_medium: 'warn',
  risk_high: 'warn', risk_shell: 'terminal', risk_network: 'globe',
  risk_fs_read: 'book-text', risk_fs_write: 'file-pen',
  risk_input: 'user', plugin: 'puzzle'
};

function iconFor(name) {
  if (!name) return null;
  return ICON_MAP[name] || null;
}

/* 造一个 <svg class="ic"><use href="#ic-xxx"/></svg> */
function iconEl(symbol, size) {
  if (!symbol) return null;
  var svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('class', 'ic' + (size === 16 ? ' ic--16' : size === 20 ? ' ic--20' : ''));
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  var use = document.createElementNS(SVG_NS, 'use');
  var href = '#ic-' + symbol;
  use.setAttribute('href', href);
  use.setAttributeNS(XLINK_NS, 'xlink:href', href);
  svg.appendChild(use);
  return svg;
}

/* 用图标替换元素内容。
   symbol 为空时保留原文本，绝不把内容清空 —— 拿不到图标也不该丢信息。 */
function setIcon(el, symbol, size) {
  if (!el) return false;
  var svg = iconEl(symbol, size);
  if (!svg) return false;
  el.textContent = '';
  el.appendChild(svg);
  return true;
}

/* 骨架屏：异步面板首次加载时的占位，替代一句「加载中…」造成的空列表闪烁。
   返回一个可直接 append 的容器；加载完成时调用方把容器清掉或整体替换即可。 */
function skeletonEl(rows) {
  var box = document.createElement('div');
  box.className = 'skeleton';
  var n = rows || 3;
  for (var i = 0; i < n; i++) {
    var r = document.createElement('div');
    r.className = 'skeleton__row' + (i === 0 ? ' skeleton__row--lg' : '');
    box.appendChild(r);
  }
  return box;
}

/* 后端 icon 取值 → 元素内容：能映射就上图标，映射不了就原文显示（不丢信息）。 */
function renderBackendIcon(el, rawValue, size) {
  if (!el) return;
  var symbol = iconFor(rawValue);
  if (symbol && setIcon(el, symbol, size)) {
    el.classList.add('has-ic');
    return;
  }
  el.classList.remove('has-ic');
  el.textContent = rawValue == null ? '' : String(rawValue);
}

var chat = $('chat'), input = $('input'), sendBtn = $('send-btn'),
    sidebar = $('sidebar'), scrim = $('scrim'), sessionList = $('session-list'),
    statusDot = $('status-dot'), modelBadge = $('model-badge'),
    chatTitle = $('chat-title'), modelInfo = $('model-info'),
    modeBtn = $('mode-btn'), modeBtnIcon = $('mode-btn-icon'),
    modeBtnLabel = $('mode-btn-label'), modeMenu = $('mode-menu'),
    settingsEl = $('settings'), settingsScrim = $('settings-scrim'),
    settingsBody = $('settings-body'), settingsNav = $('settings-nav'),
    saveStatus = $('save-status'),
    attachBtn = $('attach-btn'), attachFile = $('attach-file'),
    attachBar = $('attach-bar');

var currentSid = null, busy = false, aborter = null, timer = null, secs = 0, t0 = 0;
/* settleTimer/settleTries：中止或刷新后，轮询后端「这一轮落库了没」的定时器与计数。
   为什么需要轮询：abort 只是给后端置了个中止标记，run_agent_loop 要跑到下一个
   检查点才优雅退出并落库（ask/todos/assistant 消息）。前端若不等一下就直接渲染，
   读到的是旧 session，提问卡选项、进度条会「凭空消失」。等 inflight 落为 false
   再拉一次，状态才和后端一致。 */
var settleTimer = null, settleTries = 0;
/* 轮询上限优先用后端回传的 settleBudget（秒）：它按 totalTimeout + request_timeout
   + 余量算，前端不该自己猜。曾经硬编码 75 次 ≈ 90s，而 totalTimeout 默认就有
   180s —— 长轮次（开思考链时单次请求显著变慢，最容易撞上）会在后端还在跑的时候
   就放弃轮询、把旧画面定格下来，提问卡选项和进度条依旧「消失」。
   SETTLE_MAX_TRIES 只在拿不到后端值时兜底（315s ≈ 180+120+15，与后端默认一致）。 */
var SETTLE_INTERVAL_MS = 1200;
var SETTLE_BUDGET_SEC = 0;                                  /* 0 = 还没从后端拿到 */
var SETTLE_MAX_TRIES = Math.ceil(315000 / SETTLE_INTERVAL_MS);
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

/* ================= 附件（上传文件）=================
   流程：点「＋」选文件 → 逐个读成 base64 → POST /upload 落盘 →
   服务端回 id/路径 → 挂进 chip 列表 → 发送时只回传 id。

   为什么客户端只管 id、不管路径：磁盘名由服务端生成，路径也在服务端解析。
   前端就算被改，也构造不出「uploads 目录之外」的路径。
   上限与后端 UPLOAD_MAX_MB 保持一致，这里是早拦截，后端还会再查一遍。 */
var ATTACH_MAX_MB = 16;
var ATTACH_MAX_COUNT = 8;
/* 附件类型 → sprite symbol */
var CHIP_ICON = { image: 'image', doc: 'doc', zip: 'archive', other: 'clip' };
var pendingAtts = [];            /* [{id, name, size, isImage}]，发送后清空 */

function chipKind(f) {
  if (f.isImage) return 'image';
  if (/\.(zip|7z|rar|tar|gz)$/i.test(f.name)) return 'zip';
  if (/\.(txt|md|json|js|py|csv|log|html|css)$/i.test(f.name)) return 'doc';
  return 'other';
}

function humanSize(n) {
  n = Number(n) || 0;
  if (n < 1024) return n + 'B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + 'K';
  return (n / 1024 / 1024).toFixed(1) + 'M';
}

function renderAttachBar() {
  attachBar.innerHTML = '';
  attachBar.hidden = !pendingAtts.length;
  pendingAtts.forEach(function (f, idx) {
    var chip = document.createElement('span');
    chip.className = 'attach-chip' + (f.isImage ? ' attach-chip--img' : '');
    var ic = document.createElement('span');
    ic.className = 'attach-chip__ic';
    setIcon(ic, CHIP_ICON[chipKind(f)] || 'clip', 16);
    var nm = document.createElement('span');
    nm.className = 'attach-chip__name';
    nm.textContent = f.name;
    var sz = document.createElement('span');
    sz.className = 'attach-chip__size';
    sz.textContent = humanSize(f.size);
    var del = document.createElement('button');
    del.type = 'button';
    del.className = 'attach-chip__del';
    del.setAttribute('aria-label', '移除 ' + f.name);
    setIcon(del, 'x', 16);
    del.addEventListener('click', function () {
      pendingAtts.splice(idx, 1);
      renderAttachBar();
    });
    chip.appendChild(ic); chip.appendChild(nm); chip.appendChild(sz); chip.appendChild(del);
    attachBar.appendChild(chip);
  });
}

/* File → base64（去掉 data:...;base64, 前缀，后端只吃裸串） */
function readFileBase64(file) {
  return new Promise(function (resolve, reject) {
    var fr = new FileReader();
    fr.onload = function () {
      var s = String(fr.result || '');
      var i = s.indexOf(',');
      resolve(i >= 0 ? s.slice(i + 1) : s);
    };
    fr.onerror = function () { reject(new Error('读取失败')); };
    fr.readAsDataURL(file);
  });
}

/* 逐个上传，谁失败只影响谁 —— 不用 Promise.all，避免一个坏文件吞掉整批 */
function uploadFiles(files) {
  var list = Array.prototype.slice.call(files || []);
  if (!list.length) return;
  var room = ATTACH_MAX_COUNT - pendingAtts.length;
  if (room <= 0) { alert('一条消息最多带 ' + ATTACH_MAX_COUNT + ' 个附件'); return; }
  if (list.length > room) {
    alert('还能再加 ' + room + ' 个，多余的先不传了');
    list = list.slice(0, room);
  }
  var errors = [];
  var chain = Promise.resolve();
  list.forEach(function (f) {
    chain = chain.then(function () {
      if (f.size > ATTACH_MAX_MB * 1024 * 1024) {
        errors.push(f.name + '：' + (f.size / 1024 / 1024).toFixed(1) + 'MB 超过上限 ' + ATTACH_MAX_MB + 'MB');
        return null;
      }
      if (!f.size) { errors.push(f.name + '：是空文件'); return null; }
      return readFileBase64(f).then(function (b64) {
        return apiPost('/upload', { filename: f.name, dataBase64: b64 });
      }).then(function (r) {
        if (r && r.file) { pendingAtts.push(r.file); renderAttachBar(); }
      }).catch(function (e) {
        errors.push(f.name + '：' + ((e && e.message) || e));
      });
    });
  });
  return chain.then(function () {
    if (errors.length) alert('有文件没传上去：\n' + errors.join('\n'));
  });
}

attachBtn.addEventListener('click', function () { attachFile.click(); });
attachFile.addEventListener('change', function () {
  var files = attachFile.files;
  /* 先清空 input：同一个文件连选两次也要能触发 change */
  uploadFiles(files).then(function () { attachFile.value = ''; },
                           function () { attachFile.value = ''; });
});

function clearAtts() {
  pendingAtts = [];
  renderAttachBar();
}

function attsForSend() {
  return pendingAtts.map(function (f) {
    return { id: f.id, name: f.name };
  });
}

/* ================= 渲染 ================= */
/* 头像：昔涟侧用桌面端同一份线稿形象（cyrene-avatar-line.svg），
   用户侧用图标库的人形图标。形象是彩色/多色描边的矢量，
   走 <img> 直接引用；它不需要跟随主题变色。 */
var CYRENE_AVATAR_SRC = '/static/icons/cyrene-avatar-line.svg';

function avatarEl(cls) {
  var av = document.createElement('div');
  av.className = 'avatar ' + cls;
  if (cls === 'bot') {
    var img = document.createElement('img');
    img.className = 'avatar__img';
    img.src = CYRENE_AVATAR_SRC;
    img.alt = '';
    img.setAttribute('aria-hidden', 'true');
    av.appendChild(img);
  } else {
    av.classList.add('has-ic');
    setIcon(av, 'user', 16);
    av.setAttribute('aria-label', '你');
  }
  return av;
}

function makeBubble(cls, cont) {
  var row = document.createElement('div');
  row.className = 'msg-row ' + cls + (cont ? ' cont' : '');
  var av = avatarEl(cls);
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
  var caret = document.createElement('span');
  caret.className = 'reasoning-caret';
  setIcon(caret, 'chevron-right', 16);
  var rLabel = document.createElement('span');
  rLabel.className = 'reasoning-label';
  rLabel.appendChild(iconEl('brain', 16));
  rLabel.appendChild(document.createTextNode(' 思考过程'));
  head.appendChild(caret); head.appendChild(rLabel);
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
var TODO_ICONS = {
  pending: 'todo-pending', in_progress: 'todo-running',
  completed: 'todo-done', cancelled: 'todo-cancelled'
};

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
    ? ('任务完成 ' + done + '/' + items.length)
    : (done + '/' + items.length + ' · ' + (running ? running.content : '待办 ' + (items.length - done) + ' 项'));
  head.textContent = '';
  setIcon(head, allDone ? 'todo-done' : 'list-todo', 16);
  head.appendChild(document.createTextNode(' ' + label));
  bar.appendChild(head);

  var body = document.createElement('div');
  body.className = 'todo-bar__body';
  body.hidden = allDone;          /* 做完了默认收起，想看再点 */
  items.forEach(function (t) {
    var li = document.createElement('div');
    li.className = 'todo-item todo-item--' + (t.status || 'pending');
    var ic = document.createElement('span');
    ic.className = 'todo-item__icon';
    setIcon(ic, TODO_ICONS[t.status] || 'todo-pending', 16);
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
  title.appendChild(iconEl('help', 16));
  title.appendChild(document.createTextNode('需要你定一下'));
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
    sendTurn(lines.join('\n'), false, []);
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
  t.appendChild(iconEl('warn', 16));
  t.appendChild(document.createTextNode(' ' + (errText || '请求失败')));
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
  sendTurn(text, true, []);
}

function addBubble(text, cls, toolInfo, atts) {
  var stick = atBottom();
  var p = makeBubble(cls);
  if (cls === 'bot') mountMd(p.bub, text);
  else {
    p.bub.textContent = text;
    if (atts && atts.length) p.bub.appendChild(makeAttachBox(atts));
  }
  if (toolInfo) {
    var t = document.createElement('div'); t.className = 'tool-info'; t.textContent = toolInfo;
    p.bub.appendChild(t);
  }
  chat.appendChild(p.row);
  scrollBottom(stick);
  return p;
}

/* 用户气泡里的附件：图片给缩略图，其它给一行文件名。
   缩略图走 GET /uploads/<id> —— 服务端只放行图片扩展名，非图片不给预览。 */
function makeAttachBox(atts) {
  var box = document.createElement('div');
  box.className = 'attach-box';
  atts.forEach(function (a) {
    if (a.isImage && a.url) {
      var img = document.createElement('img');
      img.className = 'attach-thumb';
      img.src = a.url;
      img.alt = a.name || '图片';
      img.loading = 'lazy';
      box.appendChild(img);
    } else {
      var row = document.createElement('div');
      row.className = 'attach-line';
      row.appendChild(iconEl('clip', 16));
      row.appendChild(document.createTextNode(
        (a.name || a.id || '文件')
        + (a.size ? '（' + humanSize(a.size) + '）' : '')));
      box.appendChild(row);
    }
  });
  return box;
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
  var av = avatarEl('bot');
  var box = document.createElement('div'); box.className = 'thinking-box';
  box.style.flexWrap = 'wrap';
  box.style.cursor = 'pointer';
  box.title = '点一下看人家在想什么';
  box.innerHTML = '<span class="dots"><span></span><span></span><span></span></span>'
    + '<span id="think-label">昔涟正在想</span><span id="elapsed">0s</span>';
  /* 实时思考的落点：SSE 一推就往这里接。默认收着，点开才看。 */
  var live = document.createElement('div');
  live.id = 'think-live';
  live.hidden = true;
  live.style.cssText = 'flex:1 1 100%;min-width:0;white-space:pre-wrap;'
    + 'word-break:break-word;max-height:36vh;overflow:auto;'
    + 'margin-top:6px;font-size:12px;line-height:1.55;opacity:.85;';
  box.appendChild(live);
  box.addEventListener('click', function () {
    if (!live.textContent) return;
    live.hidden = !live.hidden;
  });
  row.appendChild(av); row.appendChild(box); chat.appendChild(row);
  scrollBottom(true);
  secs = 0;
  timer = setInterval(function () {
    secs++;
    var e = $('elapsed'); if (e) e.textContent = secs + 's';
  }, 1000);
}
function hideThinking() {
  stopThinkStream();
  if (timer) { clearInterval(timer); timer = null; }
  var r = $('thinking-row'); if (r) r.remove();
}
/* ================= 实时思考（SSE）=================
   等一轮回复的时候订 /chat/{sid}/stream，后端每吐一片就接到「正在想」那一行。
   连接只活在等回复这段时间：/send 一返回就关。没有 EventSource、或者中途
   断线，都只当没这回事，主流程照常等 /send 的整段结果。 */
var thinkES = null;
function startThinkStream(sid) {
  stopThinkStream();
  if (!sid || typeof EventSource !== 'function') return;
  try {
    thinkES = new EventSource('/chat/' + encodeURIComponent(sid) + '/stream');
  } catch (e) { thinkES = null; return; }
  thinkES.onmessage = function (ev) {
    var d;
    try { d = JSON.parse(ev.data); } catch (e) { return; }
    if (!d || !d.text) return;
    if (d.kind === 'reasoning') {
      var label = $('think-label');
      if (label) label.textContent = '昔涟正在想';
      var live = $('think-live');
      if (live) {
        live.hidden = false;
        live.textContent += d.text;
        live.scrollTop = live.scrollHeight;
      }
    } else if (d.kind === 'content') {
      var label2 = $('think-label');
      if (label2) label2.textContent = '昔涟正在写';
    }
  };
  thinkES.onerror = function () { /* 断线不当错误：照常等 /send */ };
}
function stopThinkStream() {
  if (!thinkES) return;
  try { thinkES.close(); } catch (e) {}
  thinkES = null;
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
  var m = findMode(mode) || { id: mode, label: mode, icon: mode };
  if (modeBtnIcon) renderBackendIcon(modeBtnIcon, m.icon || m.id, 18);
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
    /* 后端 icon 是语义 id；映射不到就用模式 id 兜底，再不行才显原文 */
    renderBackendIcon(ic, m.icon || m.id, 18);
    var tx = document.createElement('div'); tx.className = 'mode-item-text';
    var lb = document.createElement('div'); lb.className = 'mode-item-label';
    lb.textContent = m.label || m.id;
    var ds = document.createElement('div'); ds.className = 'mode-item-desc';
    ds.textContent = m.desc || '';
    tx.appendChild(lb); tx.appendChild(ds);
    it.appendChild(ic); it.appendChild(tx);
    if (m.id === currentMode) {
      var ck = document.createElement('span'); ck.className = 'mode-item-check';
      setIcon(ck, 'check', 16);
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
    empty.className = 'session-empty';
    /* 空态用迷迷形象（与桌面端同一份资产）代替一句干巴巴的提示 */
    var eImg = document.createElement('img');
    eImg.src = '/static/icons/mimi.png';
    eImg.alt = '';
    eImg.setAttribute('aria-hidden', 'true');
    empty.appendChild(eImg);
    empty.appendChild(document.createTextNode('还没有对话'));
    frag.appendChild(empty);
  }
  arr.forEach(function (s) {
    var d = document.createElement('div');
    d.className = 'session-item' + (s.id === current ? ' active' : '');
    d.dataset.sid = s.id;
    var t = document.createElement('span'); t.className = 't'; t.textContent = s.title || '新对话';
    var x = document.createElement('span'); x.className = 'del';
    setIcon(x, 'x', 16);
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
      /* 历史里的附件同样回放：图片给缩略图。附件 id 认不出时后端不会给这
         条消息带 attachments，所以这里 m.attachments 有值就等于文件还在。 */
      if (m.attachments && m.attachments.length) {
        pu.bub.appendChild(makeAttachBox(m.attachments));
      }
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
/* 渲染一次 GET /chat 的回包。renderChat 内部 chat.innerHTML='' 全量重建，
   所以重复调用幂等 —— settle 轮询里反复 applyChatData 不会叠加气泡或提问卡。 */
function applyChatData(d) {
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
}

function stopSettle() {
  if (settleTimer) { clearTimeout(settleTimer); settleTimer = null; }
  settleTries = 0;
}

/* 中止 / 刷新后，后端那一轮可能还在后台收尾落库（assistant 消息、pendingAsk、
   todos）。此时 GET /chat 读到的是旧状态，直接渲染就会让「提问卡选项、进度条
   凭空消失」。这里轮询等 inflight 落为 false，每轮都用 applyChatData 全量重渲染，
   后端一落库，选项 / 进度条就回来了。幂等，不会叠加。 */
function settleAfterLoad(sid) {
  stopSettle();
  function poll() {
    settleTries++;
    /* 用户切走了、或又发了新消息 → 交给新流程，别再刷这个会话 */
    if (sid !== currentSid || busy) { stopSettle(); return; }
    api('/chat/' + encodeURIComponent(sid)).then(function (d) {
      if (sid !== currentSid || busy) { stopSettle(); return; }
      /* 后端每次都会带上「该等多久」，用它而不是前端写死的值 */
      if (d.settleBudget) SETTLE_BUDGET_SEC = d.settleBudget;
      var limit = SETTLE_BUDGET_SEC
        ? Math.ceil(SETTLE_BUDGET_SEC * 1000 / SETTLE_INTERVAL_MS)
        : SETTLE_MAX_TRIES;
      /* 还在后台跑且预算没烧完：先不动 DOM（保留「正在收尾」提示，也避免半截
         状态闪一下），继续等。等 inflight 落 false 再一次性渲染真实内容。 */
      if (d.inflight && settleTries < limit) {
        settleTimer = setTimeout(poll, SETTLE_INTERVAL_MS);
        return;
      }
      var waitedSec = Math.round(settleTries * SETTLE_INTERVAL_MS / 1000);
      applyChatData(d);
      stopSettle();
      /* 预算烧完了后端还在跑（totalTimeout 被调大、或单次请求异常慢）。
         此前这里静默定格在旧画面，用户看到的就是「选项/进度条没了」，只能手动
         刷新 —— 现在明确告诉他后端还在收尾，并给一个就地重等的入口。
         已等秒数必须在 stopSettle 之前取：它会把 settleTries 归零。 */
      if (d.inflight) addSettleGiveUpNotice(sid, waitedSec);
    }).catch(function () { stopSettle(); });
  }
  /* 首次稍等一下再查，给后端一点落库时间，别立刻又读到「还在跑」 */
  settleTimer = setTimeout(poll, 800);
}

/* 轮询到上限而后端仍在生成时的提示。必须紧跟在 applyChatData 之后调用 ——
   renderChat 内部 chat.innerHTML='' 全量重建，先追加的节点会被清掉。 */
function addSettleGiveUpNotice(sid, waitedSec) {
  var p = makeBubble('bot');
  p.bub.textContent = '（后端这一轮还在收尾，已经等了 ' + waitedSec + 's。'
    + '结果落库后才会显示，不用刷新页面。）';
  var acts = document.createElement('div'); acts.className = 'ask-box__acts';
  var btn = document.createElement('button');
  btn.type = 'button'; btn.className = 'btn btn--primary btn--sm';
  btn.textContent = '再等一会儿';
  btn.addEventListener('click', function () {
    /* 切走了或已经在生成中就别重启，交给新流程 */
    if (sid !== currentSid || busy) { p.row.remove(); return; }
    p.row.remove();          /* 提示是临时的，重等一轮就不该留在历史里 */
    settleAfterLoad(sid);    /* stopSettle 会把 settleTries 归零，能等满一个新预算 */
  });
  acts.appendChild(btn);
  p.bub.appendChild(acts);
  var stick = atBottom();
  chat.appendChild(p.row);
  scrollBottom(stick);
}

function loadChat() {
  if (!currentSid) return Promise.resolve();
  stopSettle();
  return api('/chat/' + encodeURIComponent(currentSid)).then(function (d) {
    applyChatData(d);
    /* 后端这一轮还在后台跑（刷新时常见，或刚点过停止）：先渲染已有历史，
       再轮询等它落库后重渲染，选项 / 进度条才不会停在旧画面。 */
    if (d.inflight) settleAfterLoad(currentSid);
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
  setIcon(sendBtn, on ? 'square' : 'arrow-up', 20);
  sendBtn.setAttribute('aria-label', on ? '停止等待' : '发送');
  sendBtn.disabled = false;
}

function onSend() {
  if (busy) {
    /* 「停止」= 两件事，缺一不可：
       1) POST /chat/<sid>/abort 通知后端置中止标记 —— run_agent_loop 会在下一个
          检查点优雅退出并落库（assistant 消息 / pendingAsk / todos）。不通知的话
          后端一无所知，继续跑到 totalTimeout，这一轮迟迟不落库，刷新也看不到。
       2) aborter.abort() 断开本地 fetch —— 前端立刻从转圈中解放，不干等后端。
       断开后 sendTurn 的 catch 走 AbortError 分支，触发 settleAfterLoad 轮询后端
       落库状态，把这一轮的提问卡 / 进度条自动捞回来。 */
    if (currentSid) {
      apiPost('/chat/' + encodeURIComponent(currentSid) + '/abort', {})
        .catch(function () { /* 中止通知失败也不影响断本地连接 */ });
    }
    if (aborter) { aborter.abort(); aborter = null; }
    return;
  }
  var text = input.value.trim();
  var atts = attsForSend();
  if (!text && !atts.length) return;      /* 空消息 + 没附件 = 没什么可发的 */
  input.value = ''; input.style.height = 'auto';

  addBubble(text, 'user', null, atts);
  clearAtts();
  sendTurn(text, false, atts);
}

/* 真正发一轮请求。isRetry=true 时后端不重复追加 user 消息（它还在库里），
   前端也不再画一个用户气泡。 */
function sendTurn(text, isRetry, atts) {
  setBusy(true);
  showThinking();
  startThinkStream(currentSid);
  /* 新一轮开始：先停掉可能还在跑的 settle 轮询，避免它和这一轮的渲染交错
     （poll 里的 applyChatData 是全量重建，和 sendTurn 成功后的 addSegmented
     抢 DOM 会让气泡 / 提问卡错乱）。 */
  stopSettle();
  aborter = new AbortController();
  t0 = Date.now();

  api('/chat/' + encodeURIComponent(currentSid) + '/send', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message: text, retry: !!isRetry,
                           attachments: atts || [] }),
    signal: aborter.signal
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
          t.appendChild(iconEl('settings', 16));
          t.appendChild(document.createTextNode(' ' + r.tool_line + '\n'));
          t.appendChild(iconEl('clip', 16));
          t.appendChild(document.createTextNode(' ' + (r.tool_result || '')));
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
      msg = '（已停止等待，耗时 ' + Math.round((Date.now() - t0) / 1000)
        + 's。这一轮可能还在后台收尾，稍等会自动刷新出结果。）';
      /* 用户主动中止：后端收到 /abort 后会尽快落库这一轮。启动 settle 轮询，
         等 inflight 落 false 就重渲染，把这一轮的提问卡 / 进度条捞回来，
         不用让用户手动刷新。 */
      if (currentSid) settleAfterLoad(currentSid);
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
  { key: 'vision',     label: '视觉' },
  { key: 'mode',       label: '模式' },
  { key: 'reasoning',  label: '思考' },
  { key: 'tools',      label: '工具' },
  { key: 'plugins',    label: '插件' },
  { key: 'memory',     label: '记忆' },
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
    loadUiSkins();
    openUiStream();
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
  applyUiSkin();
}

/* ---------- 插件皮肤 ---------- */
/* 皮肤由插件在 manifest 里声明 uiSkin，用户在设置里挑一套。
   这里是故意不做内容过筛的：插件本来就是任意 Node 代码，CSS 这条路拦不住它。
   能收得住的地方在别处 —— 只有用户挑中的那套才注入，随时能点回「不用皮肤」。 */
var UI_SKINS = null;
var uiSkinsLoading = false;

/* 编辑器以外的地方也会调，放在前面声明 */

function loadUiSkins(force) {
  if (uiSkinsLoading) return Promise.resolve();
  if (UI_SKINS && !force) return Promise.resolve();
  uiSkinsLoading = true;
  return api('/ui/skins').then(function (d) {
    UI_SKINS = d && d.skins ? d : { skins: [], active: '' };
    applyUiSkin();
    if (activeTab === 'appearance') renderSettingsPanel();
  }).catch(function () {
    UI_SKINS = { skins: [], active: '' };
  }).then(function () { uiSkinsLoading = false; });
}

function applyUiSkin() {
  var el = document.getElementById('cy-ui-skin');
  var want = (SETTINGS && SETTINGS.appearance && SETTINGS.appearance.uiSkin) || '';
  var s = null;
  if (want && UI_SKINS) {
    s = (UI_SKINS.skins || []).filter(function (x) { return x.id === want; })[0] || null;
  }
  /* 图层顺序：用户挑的那套皮肤打底，插件的运行时覆盖按插件名叠在上面。
     后一层能盖前一层，这样插件想「在原皮肤上再压一层」也做得到。 */
  var layers = [];
  if (s) layers.push(s);
  if (UI_SKINS && UI_SKINS.override) {
    (UI_SKINS.override || []).forEach(function (o) { layers.push(o); });
  }
  if (!layers.length) { if (el) el.remove(); return; }
  for (var i = layers.length - 1; i >= 0; i--) {
    if (layers[i].base) {
      document.documentElement.setAttribute('data-ui-theme', layers[i].base);
      break;
    }
  }
  var css = '';
  layers.forEach(function (L) {
    var tk = L.tokens || {};
    var decl = Object.keys(tk).map(function (k) { return k + ':' + tk[k] + ';'; }).join('');
    if (decl) css += ':root{' + decl + '}\n';
    if (L.css) css += L.css + '\n';
  });
  if (!css) { if (el) el.remove(); return; }
  if (!el) {
    el = document.createElement('style');
    el.id = 'cy-ui-skin';
    document.head.appendChild(el);
  }
  el.textContent = css;
}

/* 界面事件流：插件改了皮肤，这里立刻收到，不用用户去重开设置面板。
   连不上就静默重试 —— 它只是个加速器，拿不到也不影响别的功能。 */
var uiES = null;
function openUiStream() {
  if (typeof EventSource !== 'function' || uiES) return;
  try {
    uiES = new EventSource('/ui/stream');
  } catch (e) { uiES = null; return; }
  uiES.onmessage = function (ev) {
    var d = null;
    try { d = JSON.parse(ev.data); } catch (e) { return; }
    if (!d || d.kind !== 'skin') return;
    if (UI_SKINS && d.rev === UI_SKINS.rev) return;
    UI_SKINS = null;
    loadUiSkins(true);
  };
  uiES.onerror = function () {
    /* 断了自己重连（EventSource 本来就会），这里只保证不反复 new */
  };
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
  setIcon(ico, mode === 'restart' ? 'refresh-cw' : 'square', 20);
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
  /* 设置还没拉回来时先铺骨架，别让面板空着闪一下 */
  if (!SETTINGS) { settingsBody.appendChild(skeletonEl(4)); return; }
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
    var prev = document.createElement('span');
    prev.className = 'theme-card__preview';
    prev.setAttribute('aria-hidden', 'true');
    prev.appendChild(document.createElement('i'));
    prev.appendChild(document.createElement('i'));
    prev.appendChild(document.createElement('i'));
    var tLabel = document.createElement('span');
    tLabel.className = 'theme-card__label';
    tLabel.textContent = pair[1];
    var tTick = document.createElement('span');
    tTick.className = 'theme-card__tick';
    tTick.setAttribute('aria-hidden', 'true');
    setIcon(tTick, 'check', 16);
    tLabel.appendChild(tTick);
    b.appendChild(prev); b.appendChild(tLabel);
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

  /* ---------- 插件皮肤 ---------- */
  var skins = (UI_SKINS && UI_SKINS.skins) || [];
  var curSkin = ap.uiSkin || '';
  var sg = document.createElement('div'); sg.className = 'theme-grid';
  [{ id: '', name: '不用皮肤', plugin: '' }].concat(skins).forEach(function (s) {
    var b = document.createElement('button');
    b.type = 'button';
    b.className = 'theme-card' + (curSkin === s.id ? ' is-active' : '');
    b.setAttribute('aria-pressed', String(curSkin === s.id));
    var lb = document.createElement('span');
    lb.className = 'theme-card__label';
    lb.textContent = s.name + (s.plugin ? '（' + s.plugin + '）' : '');
    b.appendChild(lb);
    b.addEventListener('click', function () {
      Array.prototype.forEach.call(sg.children, function (c) {
        c.classList.remove('is-active');
        c.setAttribute('aria-pressed', 'false');
      });
      b.classList.add('is-active');
      b.setAttribute('aria-pressed', 'true');
      if (UI_SKINS) UI_SKINS.active = s.id;
      applyUiSkin();
      saveSettings({ appearance: { uiSkin: s.id } }, true);
    });
    sg.appendChild(b);
  });
  var skinSec = section('插件皮肤', '插件在 manifest 里声明 uiSkin，这里挑一套；只列已装的',
    sg);
  host.appendChild(skinSec);
  var skinNote = document.createElement('div');
  skinNote.className = 'cy-settings-general__notice';
  skinNote.style.cssText = 'padding:4px 14px 10px';
  skinNote.textContent = skins.length
    ? '皮肤可以直接注入 CSS，它会盖住内置样式。挑之前先确认那个插件是你信得过的。'
    : '还没有插件声明 uiSkin —— 装了带皮肤的插件，这里就会冒出来。';
  skinSec.appendChild(skinNote);
  var skinBad = skins.filter(function (s) { return s.error; });
  if (skinBad.length) {
    var bw = document.createElement('div');
    bw.className = 'alert alert--warn';
    bw.textContent = skinBad.map(function (s) { return s.name + '：' + s.error; }).join('；');
    skinSec.appendChild(bw);
  }
  var skinActs = document.createElement('div');
  skinActs.style.cssText = 'display:flex;gap:8px;padding:10px 14px 0';
  skinActs.appendChild(btn('重新扫描皮肤', function () {
    UI_SKINS = null;
    loadUiSkins(true);
  }, 'btn--ghost btn--sm'));
  skinSec.appendChild(skinActs);

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

  /* 主模型多模态三选。对齐桌面端 model-settings.multimodal 的语义：
     auto 跟随实测探测（端点收不下图就自动走视觉转述），
     on   强制按能收图处理，off  强制走转述。
     放在「模型」这个 tab 里而不是视觉 tab：这是主模型的能力声明，
     视觉 tab 只管那套独立端点。 */
  c.appendChild(row('图片直传',
    '主模型能否直接收图。"自动" 以实测为准：带图请求失败过就改走视觉转述',
    segmented(m.multimodal || 'auto', [
      { value: 'auto', label: '自动' },
      { value: 'on',   label: '强制开启' },
      { value: 'off',  label: '强制关闭' }
    ], function (v) { saveSettings({ model: { multimodal: v } }); }), { stack: true }));

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

/* ================= 视觉（独立视觉模型）=================
   这一页管的是「主模型收不了图时，谁来替她看图」。
   设计骨架与桌面端 Cyrene-Agent 的 image-router 一致：
   结论只有三种（图片直传 / 交独立视觉转述 / 看不了），
   而且这个结论必须能在这页上被看见 —— 不然用户没法判断问题出在哪。 */
PANELS.vision = function (host, S) {
  var v = S.vision || {};

  /* 顶部：路由状态行。对齐桌面端「不允许模糊状态」那条设计 ——
     与其让用户在两个 tab 之间猜，不如把当前结论直接摊开。 */
  var st = card();
  var routeBox = document.createElement('div');
  routeBox.className = 'vision-route';
  st.appendChild(routeBox);
  host.appendChild(section('当前状态', '服务端按这个结论决定图片怎么走', st));

  function routeText(d) {
    var r = (d && d.route) || {};
    var modes = {
      direct:  ['图片直传', '主模型自己收图，不经过独立视觉模型。'],
      caption: ['独立视觉转述', '主模型收不了图，图片交给下面配置的视觉模型转成文字。'],
      reject:  ['看不了图', r.reason || '主模型不是多模态，且未配置独立视觉模型。']
    };
    return modes[r.mode] || ['未知', ''];
  }

  function paintStatus() {
    routeBox.textContent = '读取中…';
    fetch('/vision/status', { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        var t = routeText(d);
        routeBox.innerHTML = '';
        var big = document.createElement('div');
        big.className = 'vision-route__mode' +
          (d.route && d.route.mode === 'reject' ? ' is-bad' : ' is-ok');
        big.textContent = t[0];
        var sub = document.createElement('div');
        sub.className = 'vision-route__why';
        sub.textContent = t[1];
        routeBox.appendChild(big); routeBox.appendChild(sub);

        var meta = document.createElement('div');
        meta.className = 'vision-route__meta';
        var items = [
          '主模型图片直传：' + (d.multimodal || 'auto') +
            (d.multimodalEnabled ? '（当前按可收图处理）' : '（当前按收不了图处理）'),
          '视觉端点：' + (d.ready ? '已就绪' : '未配置完整'),
          '描述缓存：' + ((d.cache && d.cache.count) || 0) + ' / ' +
            ((d.cache && d.cache.max) || 0) + ' 条',
          '单图上限：' + Math.round(((d.maxBytes || 0) / 1024 / 1024) * 10) / 10 + ' MB'
        ];
        items.forEach(function (x) {
          var li = document.createElement('div');
          li.textContent = x;
          meta.appendChild(li);
        });
        if (d.last) {
          var last = document.createElement('div');
          last.textContent = d.last.ok
            ? ('上次调用：成功' + (d.last.cached ? '（命中缓存）' : '') +
               (d.last.atText ? ' · ' + d.last.atText : ''))
            : ('上次调用：失败 · ' + (d.last.error || ''));
          meta.appendChild(last);
        }
        routeBox.appendChild(meta);
      })
      .catch(function () {
        routeBox.textContent = '读不到状态（服务可能正在重启）';
      });
  }
  paintStatus();

  /* 端点配置 */
  var c = card();
  c.appendChild(row('启用独立视觉模型', '关掉后整个子系统退场，图片仍会落盘但不转述',
    toggle(!!v.enabled, function (on) {
      saveSettings({ vision: { enabled: on } }, true).then(paintStatus);
    }, '启用独立视觉模型')));

  c.appendChild(row('接口地址', 'OpenAI 兼容格式的 Base URL，与主模型可以不是同一家。'
    + '填到 /v1 就行，直接把完整端点（…/v1/chat/completions）粘进来也认',
    textInput(v.api_base, function (val) {
      saveSettings({ vision: { api_base: val } }, true);
    }, { mono: true, placeholder: 'https://…/v1', label: '视觉接口地址' }),
    { stack: true }));

  /* API Key 只写不回显：服务端只回 api_key_set 布尔 */
  var kwrap = document.createElement('div'); kwrap.className = 'reveal-wrap';
  var ki = document.createElement('input');
  ki.className = 'form-input form-input--mono'; ki.type = 'password';
  ki.placeholder = v.api_key_set ? '已配置（留空则不修改）' : '尚未配置';
  ki.setAttribute('aria-label', '视觉 API Key');
  var kshow = btn('显示', function () {
    ki.type = ki.type === 'password' ? 'text' : 'password';
    kshow.textContent = ki.type === 'password' ? '显示' : '隐藏';
  }, 'btn--sm');
  kwrap.appendChild(ki); kwrap.appendChild(kshow);
  c.appendChild(row('API Key', '不会回显已保存的值；留空表示不修改',
    kwrap, { stack: true }));
  ki.addEventListener('change', function () {
    if (!ki.value.trim()) return;
    saveSettings({ vision: { api_key: ki.value.trim() } }, true)
      .then(function () {
        ki.value = '';
        ki.placeholder = '已配置（留空则不修改）';
        paintStatus();
      });
  });

  c.appendChild(row('模型名称', '支持图片输入的模型，例如各家 VLM',
    textInput(v.model, function (val) {
      saveSettings({ vision: { model: val } }, true);
    }, { mono: true, placeholder: '你的视觉模型', label: '视觉模型名称' }),
    { stack: true }));

  c.appendChild(row('上传时自动转述',
    '开：发图时当场转述一次，她立刻看得见。关（默认）：只给路径，'
    + '她想看时用 read_image 自己调 —— 省一次视觉调用',
    toggle(!!v.autoCaption, function (on) {
      saveSettings({ vision: { autoCaption: on } }, true);
    }, '上传时自动转述')));

  c.appendChild(row('提供看图工具',
    '给她 read_image（本地图）与 read_image_url（网络图）两个工具。'
    + '关掉后这两个工具不会出现在工具清单里',
    toggle(v.toolEnabled !== false, function (on) {
      saveSettings({ vision: { toolEnabled: on } }, true);
    }, '提供看图工具')));

  host.appendChild(section('视觉端点',
    '与主模型完全独立的一套配置。主模型能收图时这里不会被用到', c));

  /* 参数 */
  var c3 = card();
  c3.appendChild(row('请求超时', '秒。转述是附带动作，不必跟主模型一样长',
    slider(10, 300, 5, v.request_timeout != null ? v.request_timeout : 60,
      function (val) { saveSettings({ vision: { request_timeout: val } }); },
      function (val) { return val + 's'; }), { stack: true }));
  c3.appendChild(row('单图上限', 'MB。超过就不送视觉模型，只落盘',
    slider(1, 16, 0.5, v.maxMb != null ? v.maxMb : 4,
      function (val) { saveSettings({ vision: { maxMb: val } }); },
      function (val) { return val + ' MB'; }), { stack: true }));
  c3.appendChild(row('描述缓存', '分钟。同一张图同一个问题在有效期内不重复请求',
    slider(0, 120, 5, v.cacheTtlMin != null ? v.cacheTtlMin : 30,
      function (val) { saveSettings({ vision: { cacheTtlMin: val } }); },
      function (val) { return val === 0 ? '不缓存' : val + ' 分钟'; }), { stack: true }));
  host.appendChild(section('参数', null, c3));

  /* 自检：选图 → 上传 → /vision/test，把结果原文摊出来 */
  var c4 = card();
  var fileInput = document.createElement('input');
  fileInput.type = 'file'; fileInput.accept = 'image/*';
  fileInput.style.display = 'none';
  c4.appendChild(fileInput);

  var testOut = document.createElement('div');
  testOut.className = 'vision-testout';
  testOut.textContent = '（还没有测试结果）';

  var picking = btn('选择图片测试', function () { fileInput.click(); });
  var actions = document.createElement('div');
  actions.className = 'vision-actions';
  actions.appendChild(picking);
  actions.appendChild(btn('清空描述缓存', function () {
    apiPost('/vision/cache/clear').then(function (d) {
      testOut.textContent = '已清空 ' + ((d && d.cleared) || 0) + ' 条缓存描述';
      paintStatus();
    });
  }, 'btn--ghost'));
  c4.appendChild(actions);
  c4.appendChild(testOut);

  fileInput.addEventListener('change', function () {
    var f = fileInput.files && fileInput.files[0];
    if (!f) return;
    testOut.textContent = '读取文件…';
    readFileBase64(f).then(function (b64) {
      testOut.textContent = '上传中…';
      return apiPost('/upload', { filename: f.name, dataBase64: b64 });
    }).then(function (up) {
      if (!up || !up.file || !up.file.id) throw new Error('上传没有返回 id');
      testOut.textContent = '正在让视觉模型看图…（最长 ' +
        (v.request_timeout != null ? v.request_timeout : 60) + ' 秒）';
      return apiPost('/vision/test', { id: up.file.id });
    }).then(function (d) {
      testOut.textContent = (d && d.text) || '（没有返回内容）';
      paintStatus();
    }).catch(function (e) {
      /* 转述失败时后端回 502，apiPost 会抛错 —— 但 body 里的 text
         才是真正有用的东西（"[错误·网络] …" 这种可读原因），
         所以优先从 payload 里取，而不是只显示 HTTP 状态码。 */
      var p = e && e.payload;
      testOut.textContent = (p && p.text)
        ? p.text
        : ('测试失败：' + (e && e.message ? e.message : e));
      paintStatus();
    }).then(function () { fileInput.value = ''; });
  });

  host.appendChild(section('测试识图',
    '选一张本地图片，当场走一遍完整链路（上传 → 转述 → 回显）', c4));
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
        renderBackendIcon(ic, t.icon || t.id, 16);
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
      setIcon(ic, 'sparkles', 16);
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

/* ================= 插件（P7） ================= */
/* 后端契约见 cyrene_web.py 的 _plugin_overview / market_overview：
   GET /plugins            → {host, plugins[], tools[]}
   GET /plugins/market     → {ok, source, fellBack, plugins[], counts, stale, error}
   GET /plugins/<id>/logs  → {state, error, logs[], logsLost}
   POST /plugins/<id>/<enable|disable|uninstall>（同步，enable 要等 node 冷启动）
   POST /plugins/import|import-url|scan-inbox|import-inbox（导入，返回 job 后轮询）
   POST /plugins/market/<id>/install[-from-source]（返回 job 后轮询）
   GET /plugins/install-progress/<job> → {stage, done, total, message, info}
   GET /plugins/<id>/panel → 插件自带 HTML，用 iframe sandbox 承载 */

/* 插件状态七态中文化。与后端 PLUGIN_STATES 对齐，多出来的 unsupported 也在这。 */
var PLUGIN_STATE_ZH = {
  not_installed: '未安装', installed: '已安装', starting: '启动中',
  running: '运行中', stopping: '停止中', failed: '启动失败',
  crashed: '已崩溃', unsupported: '不支持'
};
function pluginStateZh(s) { return PLUGIN_STATE_ZH[s] || s || '未知'; }
/* 状态徽标配色：复用 tool-card__badge，再叠一个语义 class。
   running 绿、failed/crashed 红、其余灰。CSS 里没有的 class 就靠内联兜底。 */
function pluginStateColor(s) {
  if (s === 'running') return '#2e9e5b';
  if (s === 'failed' || s === 'crashed') return '#d9534f';
  if (s === 'unsupported') return '#c8860d';
  if (s === 'starting' || s === 'stopping') return '#4a90d9';
  return '#8a8f98';
}

/* 轮询一个安装 job 的进度，直到 done/failed 或超时。
   导入与市场安装都是「后端丢后台线程、立刻回 job key」的异步模型，
   前端靠这个轮询驱动进度条。240 次 × 1s ≈ 4 分钟上限，够下 32MB 的包。 */
function pluginPollJob(job, onTick, onEnd) {
  var tries = 0;
  var iv = setInterval(function () {
    tries++;
    api('/plugins/install-progress/' + encodeURIComponent(job)).then(function (p) {
      if (onTick) onTick(p);
      if (p.stage === 'done' || p.stage === 'failed') {
        clearInterval(iv); if (onEnd) onEnd(p, false);
      } else if (tries > 240) {
        clearInterval(iv);
        if (onEnd) onEnd({ stage: 'failed', message: '等待超时（超过 4 分钟）' }, true);
      }
    }).catch(function () {
      /* 单次查询失败不中断（后端可能正好在重启），只在超时后收尾 */
      if (tries > 240) {
        clearInterval(iv);
        if (onEnd) onEnd({ stage: 'failed', message: '进度查询失败' }, true);
      }
    });
  }, 1000);
  return iv;
}

/* 读文件成 base64（去掉 data:...;base64, 前缀）。上传通道用。
   后端 POST /plugins/import 收的是 {dataBase64}，不是 multipart。 */
function pluginReadFileAsBase64(file) {
  return new Promise(function (resolve, reject) {
    var fr = new FileReader();
    fr.onload = function () {
      var s = String(fr.result || '');
      var i = s.indexOf(',');
      resolve(i >= 0 ? s.slice(i + 1) : s);
    };
    fr.onerror = function () { reject(new Error('读取文件失败')); };
    fr.readAsDataURL(file);
  });
}

/* 弹层：用 iframe sandbox 承载插件自带的 settingsPanel HTML。
   sandbox 只给 allow-scripts，**不给 allow-same-origin** —— 插件 HTML 是第三方
   内容，不给同源就意味着它读不到本站 cookie/localStorage，也发不出带凭据的请求。
   后端那头还叠了 CSP（connect-src 'self'），两层缺一不可。 */
function openPluginIframe(id, name) {
  var old = document.getElementById('plugin-panel-overlay');
  if (old) old.remove();
  var ov = document.createElement('div');
  ov.id = 'plugin-panel-overlay';
  ov.style.cssText = 'position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,.6);'
    + 'display:flex;align-items:center;justify-content:center;padding:16px';
  var box = document.createElement('div');
  box.style.cssText = 'background:var(--cy-card-bg,#1e1e1e);border-radius:14px;'
    + 'width:100%;max-width:520px;max-height:86vh;display:flex;flex-direction:column;'
    + 'overflow:hidden;box-shadow:0 12px 40px rgba(0,0,0,.4)';
  var bar = document.createElement('div');
  bar.style.cssText = 'display:flex;align-items:center;justify-content:space-between;'
    + 'padding:12px 14px;border-bottom:1px solid rgba(255,255,255,.08)';
  var t = document.createElement('strong');
  t.textContent = (name || id) + ' · 设置面板';
  t.style.cssText = 'font-size:15px;color:var(--cy-text,#eee)';
  var x = btn('关闭', function () { ov.remove(); }, 'btn--ghost btn--sm');
  bar.appendChild(t); bar.appendChild(x);
  var fr = document.createElement('iframe');
  fr.setAttribute('sandbox', 'allow-scripts');   // 刻意不含 allow-same-origin
  fr.src = '/plugins/' + encodeURIComponent(id) + '/panel';
  fr.style.cssText = 'flex:1;width:100%;border:0;background:#fff;min-height:320px';
  fr.title = (name || id) + ' 插件面板';
  box.appendChild(bar); box.appendChild(fr);
  ov.appendChild(box);
  ov.addEventListener('click', function (e) { if (e.target === ov) ov.remove(); });
  document.body.appendChild(ov);
}

/* 把「403 仅本机」这类后端语义错误翻译成人话，别把原始 HTTP 文本甩给用户。
   api() 抛的 Error 带 e.status 与 e.payload，这里据此分流。 */
function pluginErrMsg(e, fallback) {
  if (e && e.status === 403) {
    return (e.payload && e.payload.error) || '仅本机可执行此操作（不能在局域网其他设备上做）';
  }
  if (e && e.status === 413) {
    return (e.payload && e.payload.error) || '文件太大，超过上限';
  }
  return (e && (e.message || e.payload && e.payload.error)) || fallback || '操作失败';
}

PANELS.plugins = function (host, S) {
  var sub = 'installed';           // 子视图：已安装 / 市场
  var body = document.createElement('div');
  host.appendChild(segmented(sub, [
    { value: 'installed', label: '已安装' },
    { value: 'market', label: '插件市场' }
  ], function (v) { sub = v; body.innerHTML = ''; draw(); }));
  host.appendChild(body);

  function draw() {
    if (sub === 'market') drawMarket(body);
    else drawInstalled(body);
  }

  /* 安全检测开关落盘：走通用设置端点，只 patch plugins.securityScan 一个键。
     后端 deep_merge_settings 对 plugins 段逐键合并，不会碰 registry / secrets。 */
  function setSecurityScan(on) {
    return apiPost('/settings', { plugins: { securityScan: !!on } })
      .then(function () { draw(); })
      .catch(function (e) { alert('保存失败: ' + pluginErrMsg(e)); });
  }

  /* ---------- 已安装视图 ---------- */
  function drawInstalled(root) {
    root.textContent = '';
    root.appendChild(skeletonEl(3));
    api('/plugins').then(function (d) {
      root.innerHTML = '';
      var h = d.host || {}, list = d.plugins || [];

      /* Node 不可用横幅：置顶，因为这种情况下所有插件都跑不起来 */
      if (h.nodeAvailable === false) {
        var w = document.createElement('div'); w.className = 'alert alert--warn';
        w.textContent = '未找到 Node 运行时，插件无法启动。请在 Termux 执行 '
          + 'pkg install nodejs 后重启服务。' + (h.unsupportedReason ? '（' + h.unsupportedReason + '）' : '');
        root.appendChild(w);
      }
      if (h.enabled === false) {
        var w2 = document.createElement('div'); w2.className = 'alert alert--info';
        w2.textContent = '插件总开关已关闭（.config.json 的 plugins.enabled=false），所有插件都不会加载。';
        root.appendChild(w2);
      }

      /* 宿主状态卡 */
      var c = card();
      c.appendChild(row('Node 运行时', h.nodeAvailable ? '可用' : '缺失',
        null, { notice: h.nodeAvailable ? '' : '需要 pkg install nodejs' }));
      c.appendChild(row('运行中', (h.running || []).length + ' 个', null));
      c.appendChild(row('插件目录', String(h.dir || ''), null));
      c.appendChild(row('Plugin API', 'v' + (h.apiVersion || 1)
        + (h.hostShellExists ? '' : '（宿主壳缺失！）'), null));
      /* 插件安全检测：控制导入插件时要不要静态扫高危调用（黄标，不阻断安装）。
         关掉后新装的包不再扫描、面板也不再显示黄标；已装插件照常运行。 */
      c.appendChild(row('安全检测', h.securityScan === false
        ? '已关闭 · 导入插件时不做高危调用扫描'
        : '开启中 · 导入插件时扫描高危调用并打黄标（仅提示，不阻断安装）',
        toggle(h.securityScan !== false, function (on) { setSecurityScan(on); },
               '插件安全检测')));
      root.appendChild(section('宿主', null, c));

      /* 导入区：选文件 / 贴链接 / 扫描 inbox */
      var imp = card();
      var prog = document.createElement('div');   // 进度条挂这里
      imp.appendChild(row('从文件导入', '选一个插件 ZIP（≤' + (h.zipMaxMb || 32)
        + 'MB）。上传后后台安装，进度见下方', null));
      var fileIn = document.createElement('input');
      fileIn.type = 'file'; fileIn.accept = '.zip,application/zip';
      fileIn.className = 'form-input';
      fileIn.style.cssText = 'margin:8px 14px';
      fileIn.setAttribute('aria-label', '选择插件 ZIP');
      imp.appendChild(fileIn);
      var acts = document.createElement('div');
      acts.style.cssText = 'display:flex;gap:8px;flex-wrap:wrap;padding:4px 14px 12px';
      acts.appendChild(btn('上传安装', function () { doUpload(fileIn, prog, draw); }, 'btn--primary btn--sm'));
      acts.appendChild(btn('贴链接导入', function () { doImportUrl(prog, draw); }, 'btn--ghost btn--sm'));
      acts.appendChild(btn('扫描 inbox', function () { doScanInbox(imp, prog, draw); }, 'btn--ghost btn--sm'));
      imp.appendChild(acts);
      imp.appendChild(prog);
      var inboxHint = document.createElement('div');
      inboxHint.className = 'cy-settings-general__notice';
      inboxHint.style.cssText = 'padding:0 14px 12px';
      inboxHint.textContent = 'inbox 目录：' + (h.inboxDir || '~/cyrene/plugins_inbox')
        + (h.inboxExists ? '' : '（尚未创建）') + ' —— 用 adb push 或 Termux 把 ZIP 放进去再扫描。';
      imp.appendChild(inboxHint);
      root.appendChild(section('导入', null, imp));

      /* 插件卡片：按状态分组 */
      var groups = [
        { key: 'running', title: '运行中' },
        { key: 'installed', title: '已安装（未运行）' },
        { key: 'other', title: '异常 / 不支持' }
      ];
      var any = false;
      groups.forEach(function (g) {
        var items = list.filter(function (p) {
          if (g.key === 'running') return p.state === 'running' || p.state === 'starting';
          if (g.key === 'installed') return p.state === 'installed' || p.state === 'stopping' || p.state === 'not_installed';
          return ['failed', 'crashed', 'unsupported'].indexOf(p.state) >= 0;
        });
        if (!items.length) return;
        any = true;
        var gt = document.createElement('div'); gt.className = 'tool-panel__count';
        gt.textContent = g.title + '（' + items.length + '）';
        root.appendChild(gt);
        var grid = document.createElement('div'); grid.className = 'tool-panel__grid';
        items.forEach(function (p) { grid.appendChild(pluginCard(p, h, draw)); });
        root.appendChild(grid);
      });
      if (!any) {
        var e = document.createElement('div'); e.className = 'tool-panel__empty';
        e.textContent = '还没有安装任何插件。去「插件市场」看看，或用上面的导入。';
        root.appendChild(e);
      }

      /* 插件工具区（只读展示，开关去「工具」页做，避免两处写冲突） */
      var pt = (d.tools || []);
      if (pt.length) {
        var tc = card();
        pt.forEach(function (t) {
          tc.appendChild(row(t.id, (t.plugin ? '来自 ' + t.plugin + ' · ' : '')
            + '风险 ' + (t.risk || 'safe') + (t.enabled ? ' · 已启用' : ' · 已停用'), null));
        });
        root.appendChild(section('插件注册的工具', '开关请到「工具」页操作', tc));
      }
    }).catch(function (e) {
      root.innerHTML = '<div class="alert alert--err">插件列表读取失败: '
        + escHtml(pluginErrMsg(e)) + '</div>';
    });
  }

  /* 单个插件卡片 */
  function pluginCard(p, h, redraw) {
    var el = document.createElement('div');
    el.className = 'tool-card' + (p.state === 'running' ? '' : ' is-off');
    var ic = document.createElement('span'); ic.className = 'tool-card__icon';
    /* 插件自带 icon 时它是个图片 URL，走背景图；没有才用通用插件图标 */
    if (p.icon) {
      ic.textContent = '';
      ic.style.cssText = 'background-size:cover;background-image:url(' + p.icon + ')';
    } else {
      setIcon(ic, 'puzzle', 16);
    }
    var bd = document.createElement('div'); bd.className = 'tool-card__body';

    var nm = document.createElement('div'); nm.className = 'tool-card__name';
    nm.textContent = p.name || p.id;
    var stb = document.createElement('span'); stb.className = 'tool-card__badge';
    stb.textContent = pluginStateZh(p.state);
    stb.style.color = pluginStateColor(p.state);
    stb.style.border = '1px solid ' + pluginStateColor(p.state);
    nm.appendChild(stb);
    if (p.version) {
      var vb = document.createElement('span'); vb.className = 'tool-card__badge';
      vb.textContent = 'v' + p.version; nm.appendChild(vb);
    }
    if (h.securityScan !== false && p.risky && p.risky.length) {
      var rb = document.createElement('span'); rb.className = 'tool-card__badge';
      rb.style.color = '#c8860d';
      rb.appendChild(iconEl('warn', 16));
      rb.appendChild(document.createTextNode('高危调用'));
      rb.title = '静态扫描发现：' + p.risky.join(', ');
      nm.appendChild(rb);
    }
    if (p.uiSkin) {
      var kb = document.createElement('span'); kb.className = 'tool-card__badge';
      kb.textContent = '会改界面';
      kb.title = '这个插件声明了 uiSkin，皮肤在 设置 → 外观 里挑';
      nm.appendChild(kb);
    }
    if (p.verified === false && p.source) {
      var ub = document.createElement('span'); ub.className = 'tool-card__badge';
      ub.textContent = '未经 sha256'; ub.style.color = '#c8860d'; nm.appendChild(ub);
    }
    bd.appendChild(nm);

    var ds = document.createElement('div'); ds.className = 'tool-card__desc';
    ds.style.whiteSpace = 'normal';
    ds.textContent = p.description || '';
    bd.appendChild(ds);

    if (p.author || (p.tools && p.tools.length)) {
      var meta = document.createElement('div'); meta.className = 'tool-card__desc';
      meta.textContent = (p.author ? '作者 ' + p.author : '')
        + (p.tools && p.tools.length ? '  ·  工具 ' + p.tools.join(', ') : '');
      bd.appendChild(meta);
    }
    if (!p.supported && p.unsupportedReason) {
      var ur = document.createElement('div'); ur.className = 'tool-card__desc';
      ur.style.color = '#d9534f'; ur.textContent = '不支持：' + p.unsupportedReason;
      bd.appendChild(ur);
    }
    if (p.error) {
      var er = document.createElement('div'); er.className = 'tool-card__desc';
      er.style.color = '#d9534f'; er.textContent = '错误：' + p.error;
      bd.appendChild(er);
    }

    /* 操作按钮行 */
    var acts = document.createElement('div');
    acts.style.cssText = 'display:flex;gap:6px;flex-wrap:wrap;margin-top:8px';

    /* 启用/停用：只有 supported 才给开关（不支持的点了必失败，体验差） */
    if (p.supported) {
      var running = (p.state === 'running' || p.state === 'starting');
      acts.appendChild(btn(running ? '停用' : '启用', function (ev) {
        var b = ev.currentTarget; b.disabled = true;
        var old = b.textContent; b.textContent = running ? '停止中…' : '启动中…';
        apiPost('/plugins/' + encodeURIComponent(p.id) + '/' + (running ? 'disable' : 'enable'))
          .then(function () { redraw(); })
          .catch(function (e) { b.disabled = false; b.textContent = old; alert(pluginErrMsg(e)); });
      }, running ? 'btn--ghost btn--sm' : 'btn--primary btn--sm'));
    }
    if (p.settingsPanel) {
      acts.appendChild(btn('面板', function () { openPluginIframe(p.id, p.name); }, 'btn--ghost btn--sm'));
    }
    acts.appendChild(btn('日志', function (ev) { toggleLogs(el, p, ev.currentTarget); }, 'btn--ghost btn--sm'));
    if (p.state !== 'not_installed') {
      acts.appendChild(btn('卸载', function () {
        if (!confirm('卸载插件「' + (p.name || p.id) + '」？插件文件会被删除，不可恢复。')) return;
        var rm = confirm('是否连同插件数据一起删除？\n\n「确定」= 连 data/ 与密钥一起删（重装要重新配置）\n「取消」= 保留数据（重装后配置还在）');
        apiPost('/plugins/' + encodeURIComponent(p.id) + '/uninstall', { removeData: rm })
          .then(function () { redraw(); })
          .catch(function (e) { alert(pluginErrMsg(e)); });
      }, 'btn--danger btn--sm'));
    }
    bd.appendChild(acts);

    el.appendChild(ic); el.appendChild(bd);
    return el;
  }

  /* 卡片内联展开日志（再点收起）。不做弹层，够用且省事。 */
  function toggleLogs(cardEl, p, btnEl) {
    var exist = cardEl.querySelector('.plugin-logs');
    if (exist) { exist.remove(); btnEl.textContent = '日志'; return; }
    btnEl.textContent = '加载中…';
    api('/plugins/' + encodeURIComponent(p.id) + '/logs?limit=200').then(function (d) {
      btnEl.textContent = '收起日志';
      var box = document.createElement('div'); box.className = 'plugin-logs';
      box.style.cssText = 'margin-top:8px';
      if (d.logsLost) {
        var lw = document.createElement('div'); lw.className = 'alert alert--warn';
        lw.textContent = '插件进程已退出，日志随内存丢失（当前状态：' + pluginStateZh(d.state) + '）。';
        box.appendChild(lw);
      }
      if (d.error) {
        var le = document.createElement('div'); le.className = 'alert alert--err';
        le.textContent = d.error; box.appendChild(le);
      }
      var pre = document.createElement('pre');
      pre.style.cssText = 'max-height:220px;overflow:auto;background:rgba(0,0,0,.25);'
        + 'padding:8px;border-radius:8px;font-size:12px;white-space:pre-wrap;'
        + 'word-break:break-all;margin:0';
      var lines = d.logs || [];
      pre.textContent = lines.length ? lines.join('\n') : '（无日志输出）';
      box.appendChild(pre);
      cardEl.appendChild(box);
    }).catch(function (e) {
      btnEl.textContent = '日志';
      alert('日志读取失败: ' + pluginErrMsg(e));
    });
  }

  /* 上传安装 */
  function doUpload(fileIn, prog, redraw) {
    var f = fileIn.files && fileIn.files[0];
    if (!f) { alert('先选一个 ZIP 文件'); return; }
    var maxMb = 32;
    if (f.size > maxMb * 1024 * 1024) {
      alert('文件 ' + (f.size / 1024 / 1024).toFixed(1) + 'MB 超过上限 ' + maxMb + 'MB');
      return;
    }
    prog.innerHTML = '';
    var bar = makeProgress(prog);
    bar.set(0, '读取文件…');
    pluginReadFileAsBase64(f).then(function (b64) {
      bar.set(0.05, '上传中…');
      return apiPost('/plugins/import', { filename: f.name, dataBase64: b64, enable: false });
    }).then(function (r) {
      watchJob(r.job, bar, redraw, fileIn);
    }).catch(function (e) {
      bar.fail(pluginErrMsg(e));
    });
  }

  /* 贴链接导入 */
  function doImportUrl(prog, redraw) {
    var url = prompt('粘贴插件 ZIP 的直链（http/https）：');
    if (!url || !url.trim()) return;
    var sha = prompt('（可选）填 sha256 强校验，留空跳过：') || '';
    prog.innerHTML = '';
    var bar = makeProgress(prog);
    bar.set(0, '提交下载任务…');
    apiPost('/plugins/import-url', { url: url.trim(), sha256: sha.trim().toLowerCase(), enable: false })
      .then(function (r) { watchJob(r.job, bar, redraw); })
      .catch(function (e) { bar.fail(pluginErrMsg(e)); });
  }

  /* 扫描 inbox → 列出候选 → 逐个可装 */
  function doScanInbox(imp, prog, redraw) {
    apiPost('/plugins/scan-inbox').then(function (d) {
      var old = imp.querySelector('.inbox-list');
      if (old) old.remove();
      var box = document.createElement('div'); box.className = 'inbox-list';
      box.style.cssText = 'padding:0 14px 12px';
      var items = d.items || [];
      if (!items.length) {
        box.innerHTML = '<div class="tool-panel__empty">inbox 里没有 ZIP：'
          + escHtml(d.dir || '') + '</div>';
        imp.appendChild(box); return;
      }
      var tt = document.createElement('div'); tt.className = 'tool-panel__count';
      tt.textContent = 'inbox 发现 ' + items.length + ' 个包';
      box.appendChild(tt);
      items.forEach(function (it) {
        var r = document.createElement('div');
        r.style.cssText = 'display:flex;align-items:center;justify-content:space-between;'
          + 'gap:8px;padding:6px 0;border-top:1px solid rgba(255,255,255,.06)';
        var lab = document.createElement('span');
        lab.style.cssText = 'font-size:13px;word-break:break-all';
        lab.textContent = it.filename + (it.id ? '  (' + it.id + (it.version ? ' v' + it.version : '') + ')' : '')
          + (it.error ? '  · ' + it.error : '') + (it.oversize ? '  · 超过上限' : '');
        var b = btn('安装', function () {
          if (it.error || it.oversize) { alert('这个包有问题：' + (it.error || '超过体积上限')); return; }
          prog.innerHTML = '';
          var bar = makeProgress(prog);
          bar.set(0, '安装 ' + it.filename + '…');
          apiPost('/plugins/import-inbox', { filename: it.filename, enable: false, deleteAfter: false })
            .then(function (rr) { watchJob(rr.job, bar, redraw); })
            .catch(function (e) { bar.fail(pluginErrMsg(e)); });
        }, 'btn--ghost btn--sm');
        r.appendChild(lab); r.appendChild(b);
        box.appendChild(r);
      });
      imp.appendChild(box);
    }).catch(function (e) { alert('扫描失败: ' + pluginErrMsg(e)); });
  }

  /* 盯着一个 job 直到结束 */
  function watchJob(job, bar, redraw, fileIn) {
    pluginPollJob(job, function (p) {
      var pct = p.total > 0 ? Math.min(1, p.done / p.total) : 0.1;
      bar.set(pct, pluginStateZh2(p.stage) + (p.message ? ' · ' + p.message : ''));
    }, function (p, timedOut) {
      if (p.stage === 'done') {
        bar.ok((p.info && p.info.info && p.info.info.id ? '已安装 ' + p.info.info.id : '安装完成')
          + ((p.info && p.info.info && p.info.info.risky && p.info.info.risky.length)
            ? '（含高危调用，见卡片黄标）' : ''));
        if (fileIn) fileIn.value = '';
        redraw();
      } else {
        var msg = p.message || '安装失败';
        if (p.info && p.info.info) {
          var ii = p.info.info;
          if (ii.expected && ii.actual) {
            msg = 'sha256 校验失败：期望 ' + ii.expected.slice(0, 12) + '… 实际 ' + ii.actual.slice(0, 12) + '…';
          } else if (typeof ii === 'string') { msg = ii; }
          else if (ii.error) { msg = ii.error; }
        }
        bar.fail(msg);
      }
    });
  }
  function pluginStateZh2(stage) {
    return { downloading: '下载中', verifying: '校验中', unpacking: '解包中',
      installing: '安装中', done: '完成', failed: '失败' }[stage] || stage || '处理中';
  }

  /* 进度条（纯内联样式，不动 settings.css） */
  function makeProgress(root) {
    var wrap = document.createElement('div');
    wrap.style.cssText = 'padding:8px 14px 12px';
    var track = document.createElement('div');
    track.style.cssText = 'height:6px;border-radius:3px;background:rgba(255,255,255,.12);overflow:hidden';
    var fill = document.createElement('div');
    fill.style.cssText = 'height:100%;width:0%;background:#e86a92;transition:width .3s';
    track.appendChild(fill);
    var txt = document.createElement('div');
    txt.style.cssText = 'font-size:12px;margin-top:6px;color:var(--cy-text-dim,#aaa);word-break:break-all';
    wrap.appendChild(track); wrap.appendChild(txt);
    root.appendChild(wrap);
    return {
      set: function (pct, msg) {
        fill.style.width = Math.max(0, Math.min(1, pct)) * 100 + '%';
        txt.style.color = 'var(--cy-text-dim,#aaa)';
        txt.textContent = msg || '';
      },
      ok: function (msg) {
        fill.style.width = '100%'; fill.style.background = '#2e9e5b'; txt.style.color = '#2e9e5b';
        txt.textContent = ''; txt.appendChild(iconEl('check', 16));
        txt.appendChild(document.createTextNode(' ' + (msg || '完成')));
      },
      fail: function (msg) {
        fill.style.background = '#d9534f'; txt.style.color = '#d9534f';
        txt.textContent = ''; txt.appendChild(iconEl('x', 16));
        txt.appendChild(document.createTextNode(' ' + (msg || '失败')));
      }
    };
  }

  /* ---------- 市场视图 ---------- */
  function drawMarket(root) {
    root.textContent = '';
    root.appendChild(skeletonEl(4));
    loadMarket(root, false);
  }
  function loadMarket(root, refresh) {
    // 守卫：市场安装是异步的，完成回调里会重新 loadMarket。若用户此刻已切走
    // tab（renderSettingsPanel 把 settingsBody.innerHTML 清空），root 会脱离文档、
    // 甚至 grid.parentNode 变 null。不拦就会在 null.innerHTML 上崩。
    if (!root || !root.isConnected) return;
    api('/plugins/market' + (refresh ? '?refresh=1' : '')).then(function (d) {
      if (!root.isConnected) return;   // 异步回来时可能已切走
      root.innerHTML = '';
      /* 顶部：源标识 + 刷新 + 搜索 */
      var bar = document.createElement('div');
      bar.style.cssText = 'display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:4px 0 8px';
      var src = document.createElement('span');
      src.className = 'tool-card__badge';
      src.textContent = '源：' + (d.source || '?') + (d.fellBack ? '（已回退）' : '')
        + (d.stale ? ' · 缓存' : '');
      src.style.color = d.fellBack || d.stale ? '#c8860d' : '#8a8f98';
      bar.appendChild(src);
      bar.appendChild(btn('刷新', function () { loadMarket(root, true); }, 'btn--ghost btn--sm'));
      root.appendChild(bar);

      if (d.error) {
        var ew = document.createElement('div');
        ew.className = 'alert ' + (d.stale ? 'alert--warn' : 'alert--err');
        ew.textContent = d.error; root.appendChild(ew);
      }
      var counts = d.counts || {};
      var cs = document.createElement('div'); cs.className = 'tool-panel__count';
      cs.textContent = '共 ' + (counts.total || 0) + ' 个 · 已装 ' + (counts.installed || 0)
        + ' · 可更新 ' + (counts.updateAvailable || 0);
      root.appendChild(cs);

      var si = document.createElement('input');
      si.className = 'tool-panel__search'; si.placeholder = '搜索市场插件…';
      si.style.cssText = 'margin:6px 0'; si.setAttribute('aria-label', '搜索市场插件');
      root.appendChild(si);
      var grid = document.createElement('div'); grid.className = 'tool-panel__grid';
      root.appendChild(grid);

      function paint() {
        var kw = si.value.trim().toLowerCase();
        var items = (d.plugins || []).filter(function (p) {
          return !kw || (p.id + ' ' + p.name + ' ' + p.description).toLowerCase().indexOf(kw) >= 0;
        });
        grid.innerHTML = '';
        if (!items.length) {
          grid.innerHTML = '<div class="tool-panel__empty">没有匹配的插件</div>'; return;
        }
        items.forEach(function (p) { grid.appendChild(marketCard(p, grid)); });
      }
      si.addEventListener('input', paint);
      paint();
    }).catch(function (e) {
      root.innerHTML = '<div class="alert alert--err">市场加载失败：'
        + escHtml(pluginErrMsg(e)) + '<br><br>可能是手机连不上 Gitee/GitHub，'
        + '或市场源暂时不可用。本地导入不受影响。</div>';
    });
  }

  function marketCard(p, grid) {
    var el = document.createElement('div');
    el.className = 'tool-card';
    var ic = document.createElement('span'); ic.className = 'tool-card__icon';
    setIcon(ic, 'package', 16);
    var bd = document.createElement('div'); bd.className = 'tool-card__body';
    var nm = document.createElement('div'); nm.className = 'tool-card__name';
    nm.textContent = p.name || p.id;
    if (p.version) {
      var vb = document.createElement('span'); vb.className = 'tool-card__badge';
      vb.textContent = 'v' + p.version; nm.appendChild(vb);
    }
    if (p.status === 'update_available') {
      var ub = document.createElement('span'); ub.className = 'tool-card__badge';
      ub.textContent = '可更新（本地 v' + (p.localVersion || '?') + '）'; ub.style.color = '#c8860d';
      nm.appendChild(ub);
    } else if (p.status === 'installed') {
      var ib = document.createElement('span'); ib.className = 'tool-card__badge';
      ib.textContent = '已安装'; ib.style.color = '#2e9e5b'; nm.appendChild(ib);
    }
    bd.appendChild(nm);
    var ds = document.createElement('div'); ds.className = 'tool-card__desc';
    ds.style.whiteSpace = 'normal';
    ds.textContent = p.description || '';
    bd.appendChild(ds);
    var meta = document.createElement('div'); meta.className = 'tool-card__desc';
    meta.textContent = (p.author ? '作者 ' + p.author + '  ·  ' : '')
      + '下载 ' + (p.downloads || 0) + (p.zipUrl ? '' : '  ·  仅源码安装');
    bd.appendChild(meta);

    var acts = document.createElement('div');
    acts.style.cssText = 'display:flex;gap:6px;flex-wrap:wrap;margin-top:8px';
    var prog = document.createElement('div');
    var installLabel = p.status === 'update_available' ? '更新' : '安装';
    if (p.zipUrl) {
      acts.appendChild(btn(installLabel, function (ev) {
        runMarketInstall('/plugins/market/' + encodeURIComponent(p.id) + '/install',
          { enable: false }, ev.currentTarget, prog, function () { loadMarket(grid.parentNode, false); });
      }, 'btn--primary btn--sm'));
    }
    /* 无 zip 的（四个官方示例）只能源码安装；有 zip 的也给一个源码入口兜底 */
    acts.appendChild(btn(p.zipUrl ? '源码安装' : '源码安装', function (ev) {
      if (!confirm('「' + (p.name || p.id) + '」将逐个拉取源码文件安装，无 sha256 校验。继续？')) return;
      runMarketInstall('/plugins/market/' + encodeURIComponent(p.id) + '/install-from-source',
        { enable: false }, ev.currentTarget, prog, function () { loadMarket(grid.parentNode, false); });
    }, 'btn--ghost btn--sm'));
    bd.appendChild(acts);
    bd.appendChild(prog);

    el.appendChild(ic); el.appendChild(bd);
    return el;
  }

  function runMarketInstall(url, body, btnEl, prog, after) {
    var old = btnEl.textContent;
    btnEl.disabled = true; btnEl.textContent = '提交中…';
    prog.innerHTML = '';
    var bar = makeProgress(prog);
    bar.set(0, '排队…');
    apiPost(url, body).then(function (r) {
      watchJob(r.job, bar, function () { btnEl.disabled = false; btnEl.textContent = old; after(); });
    }).catch(function (e) {
      btnEl.disabled = false; btnEl.textContent = old;
      bar.fail(pluginErrMsg(e));
    });
  }

  draw();
};

/* ================= 记忆（P5） ================= */
/* 四块：开关与状态 / L0 画像与 L1 近况 / L2 长期条目 / 反思日志。
   数据一次从 GET /memory/panel 拿全（见 cyrene_web.memory_panel），前端不自己拼接口。
   ⚠ L2 的「正在整理」和「没加载」必须分开说：后端拿不到锁时返回的正是空 items + busy。
     要是跟着 items.length === 0 走，面板会显示成「什么都没记」——那是在骗她。所以 busy 先判。 */
PANELS.memory = function (host, S) {
  var m = S.memory || {};

  /* 开关落盘：先改本地镜像 S.memory[key]，再 saveSettings。
     saveSettings 之后 renderSettingsPanel() 重画用的是同一份 SETTINGS，
     镜像没跟着改的话开关会「弹回原位」——reasoning 那边踩过的同一个坑。 */
  function setMem(key, value) {
    if (!S.memory) S.memory = {};
    S.memory[key] = value;
    var patch = {};
    patch[key] = value;
    saveSettings({ memory: patch }, true);
  }

  /* ---------- ① 开关与状态 ---------- */
  var cs = card();
  cs.appendChild(row('记忆总开关', '总闸。关掉后世界书不再注入，L2 也不写、不召回',
    toggle(!!m.enabled, function (on) { setMem('enabled', on); renderSettingsPanel(); },
      '记忆总开关')));
  cs.appendChild(row('L2 长期记忆', '关于他的长期条目：抽取 / 召回 / 衰减。总闸关着时这一项不起作用',
    toggle(!!m.l2Enabled, function (on) { setMem('l2Enabled', on); renderSettingsPanel(); },
      'L2 长期记忆'),
    { notice: m.enabled ? '' : '总开关还没打开' }));
  var statBox = document.createElement('div');
  cs.appendChild(statBox);
  host.appendChild(section('开关与状态', '下面的数字每次进来重新从服务端读', cs));

  /* ---------- ② L0 画像 / L1 近况 ---------- */
  var cp = card();
  var profBox = document.createElement('div');
  cp.appendChild(profBox);
  host.appendChild(section('她记住的', 'L0 是长期画像，L1 是最近这一段的状态', cp));

  /* ---------- ③ L2 长期条目 ---------- */
  var cl = card();
  var l2Box = document.createElement('div');
  cl.appendChild(l2Box);
  host.appendChild(section('长期记忆', '按时间倒序，新的在前。删掉一条就是让她忘掉这件事', cl));

  /* ---------- ④ 反思日志 ---------- */
  var cr = card();
  var logBox = document.createElement('div');
  cr.appendChild(logBox);
  host.appendChild(section('反思日志', '她自己整理记忆时留下的记录', cr));

  /* ---------- 小工具 ---------- */
  /* 毫秒时间戳（后端 _now_ms，对齐桌面端 Date.now）→ 「MM-DD HH:MM」 */
  function pad2(n) { return (n < 10 ? '0' : '') + n; }
  function fmtTime(ms) {
    var n = Number(ms);
    if (!n) return '';
    var d = new Date(n);
    if (isNaN(d.getTime())) return '';
    return pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) + ' ' +
      pad2(d.getHours()) + ':' + pad2(d.getMinutes());
  }
  function fmtNum(v) {
    var n = Number(v);
    if (v == null || v === '' || isNaN(n)) return '—';
    return String(Math.round(n * 100) / 100);
  }
  function note(text, warn) {
    var e = document.createElement('div');
    e.className = 'mem-note' + (warn ? ' mem-note--off' : '');
    e.textContent = text;
    return e;
  }
  function failBox(box, text, cls) {
    box.innerHTML = '';
    var w = document.createElement('div');
    w.className = 'alert ' + (cls || 'alert--warn');
    w.textContent = text;
    box.appendChild(w);
  }

  /* L2 三态：后端的 status 只有这三个值（见 l2_panel_data 的 counts） */
  var L2_STATUS = {
    active:   { text: '活跃', cls: 'is-active' },
    aging:    { text: '沉睡', cls: 'is-aging' },
    archived: { text: '归档', cls: 'is-archived' }
  };
  /* 反思日志类型：compression / l0_update / l1_update，别发明第四种 */
  var LOG_TYPE = { compression: '片段压缩', l0_update: '画像更新', l1_update: '近况更新' };

  var L0_FIELDS = [
    { key: 'nickname',          label: '昵称' },
    { key: 'preferredName',     label: '称呼' },
    { key: 'occupation',        label: '身份' },
    { key: 'longTermInterests', label: '长期兴趣' },
    { key: 'language',          label: '语言' },
    { key: 'permanentNote',     label: '永久备注', block: true }
  ];
  var L1_FIELDS = [
    { key: 'recentGoals',       label: '最近目标', block: true },
    { key: 'recentPreferences', label: '最近偏好', block: true },
    { key: 'currentProject',    label: '在做的事', block: true }
  ];

  /* 一个字段一格，且**可以直接改**。
     显示态：label + 值 + 「改」；编辑态：输入框 + 保存/取消。
     空值写「（空）」而不是留白 —— 留白看不出是「没填」还是「没读到」。 */
  function fieldEl(level, f, value) {
    var r = document.createElement('div');
    r.className = 'mem-field' + (f.block ? ' mem-field--block' : '');
    var k = document.createElement('div');
    k.className = 'mem-field__k';
    k.textContent = f.label;
    var v = document.createElement('div');
    v.className = 'mem-field__v';
    var s = (value == null ? '' : String(value)).trim();
    if (s) { v.textContent = s; }
    else { v.classList.add('is-empty'); v.textContent = '（空）'; }
    var act = document.createElement('div');
    act.className = 'mem-field__act';
    act.appendChild(btn('改', function () { editField(r, v, act, level, f, s); },
      'btn--ghost btn--sm'));
    r.appendChild(k); r.appendChild(v); r.appendChild(act);
    return r;
  }

  /* 把一格切成编辑态。保存只提交这一个字段；改完整页重读，以服务端的值为准
     （本地改镜像那一套对记忆库不适用——它不在 SETTINGS 里）。 */
  function editField(row, v, act, level, f, cur) {
    if (!row.isConnected) return;            /* 已经切走了就别动它 */
    var box = document.createElement('textarea');
    box.className = 'mem-field__input';
    box.value = cur;
    box.rows = f.block ? 3 : 1;
    box.placeholder = '（留空就是清掉这一项）';
    var bar = document.createElement('div');
    bar.className = 'mem-field__act';
    var save = btn('保存', function () {
      if (!row.isConnected) return;
      var patch = {};
      patch[level] = {};
      patch[level][f.key] = box.value;
      save.disabled = true;
      apiPost('/memory/profile', patch).then(function (d) {
        if (d && d.ok) { renderSettingsPanel(); return; }
        save.disabled = false;
        alert('没改成：' + ((d && d.error) || '未知原因'));
      }).catch(function (e) {
        save.disabled = false;
        alert('没改成：' + (e.message || e));
      });
    }, 'btn--sm');
    bar.appendChild(save);
    bar.appendChild(btn('取消', function () { renderSettingsPanel(); },
      'btn--ghost btn--sm'));
    row.replaceChild(box, v);
    row.replaceChild(bar, act);
    if (box.focus) box.focus();
  }

  function subHead(text, meta) {
    var h = document.createElement('div');
    h.className = 'mem-subhead';
    var k = document.createElement('span');
    k.textContent = text;
    h.appendChild(k);
    if (meta) {
      var s = document.createElement('span');
      s.className = 'mem-subhead__meta';
      s.textContent = meta;
      h.appendChild(s);
    }
    return h;
  }

  /* ---------- ① 状态 ---------- */
  function paintStatus(st) {
    statBox.innerHTML = '';
    var tri = st.states || {};
    var pol = st.policy || {};
    if (st.engineLoaded === false) {
      statBox.appendChild(note('世界书引擎没加载：条目数为 0 是真的没有，不是面板读错了。', true));
    }
    var box = document.createElement('div');
    box.className = 'mem-stats';
    var items = [
      ['世界书条目', (st.entries || 0) + ' 条'],
      ['其中永久', (st.permanent || 0) + ' 条'],
      ['三态', '活跃 ' + (tri.Active || 0) + ' · 沉睡 ' + (tri.Dormant || 0) +
        ' · 归档 ' + (tri.Archived || 0)],
      ['策略', pol.loaded
        ? ('在意 ' + (pol.care || 0) + ' 条 · 回避 ' + (pol.avoid || 0) + ' 条')
        : '未加载'],
      ['唤醒 / 衰减', '×' + fmtNum(pol.wakeScale == null ? 1 : pol.wakeScale) +
        ' / ×' + fmtNum(pol.decayScale == null ? 1 : pol.decayScale)],
      ['注入上限', (st.maxInjectChars || 0) + ' 字']
    ];
    items.forEach(function (it) {
      var r = document.createElement('div');
      r.className = 'mem-stat';
      var k = document.createElement('div'); k.className = 'mem-stat__k'; k.textContent = it[0];
      var v = document.createElement('div'); v.className = 'mem-stat__v'; v.textContent = it[1];
      r.appendChild(k); r.appendChild(v);
      box.appendChild(r);
    });
    statBox.appendChild(box);
  }

  /* ---------- ② 画像与近况 ---------- */
  function paintProfile(l0, l1) {
    profBox.innerHTML = '';
    var upd = fmtTime(l0.updatedAt);
    profBox.appendChild(subHead('L0 · 长期画像',
      (l0.isPinned ? '已钉住 · ' : '') + (upd ? '更新于 ' + upd : '还没更新过')));
    var f0 = document.createElement('div');
    f0.className = 'mem-fields';
    L0_FIELDS.forEach(function (f) { f0.appendChild(fieldEl('l0', f, l0[f.key])); });
    profBox.appendChild(f0);

    profBox.appendChild(subHead('L1 · 近况', '走过 ' + (l1.roundCount || 0) + ' 轮对话'));
    var f1 = document.createElement('div');
    f1.className = 'mem-fields';
    L1_FIELDS.forEach(function (f) { f1.appendChild(fieldEl('l1', f, l1[f.key])); });
    profBox.appendChild(f1);
  }

  /* ---------- ③ L2 列表 ---------- */
  function paintL2(d) {
    l2Box.innerHTML = '';
    /* ⚠ 次序不能换：busy 时后端给的就是空列表，先判列表长度就会误报成「什么都没记」。 */
    if (d.l2Busy === true) {
      l2Box.appendChild(note('记忆库正在整理，过一会儿再看。'));
      return;
    }
    if (d.l2Available === false) {
      l2Box.appendChild(note('记忆模块未加载：设置里打开「L2 长期记忆」之后才会有条目。', true));
      return;
    }
    var items = d.l2 || [];
    var co = d.counts || {};
    var head = document.createElement('div');
    head.className = 'mem-list__head';
    head.textContent = '共 ' + (co.total == null ? items.length : co.total) + ' 条 · 活跃 '
      + (co.active || 0) + ' · 沉睡 ' + (co.aging || 0) + ' · 归档 ' + (co.archived || 0);
    l2Box.appendChild(head);
    if (!items.length) {
      l2Box.appendChild(note('还没有记下什么。等她多聊几句，条目会自己长出来。'));
      return;
    }
    var list = document.createElement('div');
    list.className = 'mem-list';
    items.forEach(function (it) { list.appendChild(l2El(it)); });
    l2Box.appendChild(list);
  }

  function l2El(it) {
    var line = document.createElement('div');
    line.className = 'mem-l2-row';
    var main = document.createElement('div');
    main.className = 'mem-l2-row__main';
    var txt = document.createElement('div');
    txt.className = 'mem-l2-row__text';
    txt.textContent = String(it.content || '');
    main.appendChild(txt);
    var meta = document.createElement('div');
    meta.className = 'mem-l2-row__meta';
    var st = L2_STATUS[it.status] || { text: String(it.status || '未知'), cls: '' };
    var tag = document.createElement('span');
    tag.className = 'mem-tag ' + st.cls;
    tag.textContent = st.text;
    meta.appendChild(tag);
    if (it.isPinned) {
      var pin = document.createElement('span');
      pin.className = 'mem-tag is-pin';
      pin.textContent = '钉住';
      meta.appendChild(pin);
    }
    var bits = [
      '权重 ' + fmtNum(it.weight),
      '激活 ' + fmtNum(it.activation),
      '召回 ' + (it.recallCount == null ? 0 : it.recallCount) + ' 次',
      fmtTime(it.createdAt)
    ];
    bits.forEach(function (x) {
      if (!x) return;
      var s = document.createElement('span');
      s.textContent = x;
      meta.appendChild(s);
    });
    main.appendChild(meta);
    line.appendChild(main);
    var del = btn('删除', function () { doForget(it, del); }, 'btn--danger btn--sm');
    del.setAttribute('aria-label', '删除这条记忆');
    line.appendChild(del);
    return line;
  }

  /* 删除是真删（后端 l2_forget 会把 DMAE 状态行一起清），所以先问一句。
     成功后就地整块重绘：数字和列表一起更新，不用手写「本地摘掉一个 li」的乐观更新。 */
  function doForget(it, btnEl) {
    if (!confirm('让她忘掉这条？删掉之后不再参与召回。')) return;
    btnEl.disabled = true;
    btnEl.textContent = '删除中…';
    apiPost('/memory/l2/forget', { id: it.id }).then(function () {
      if (!host.isConnected) return;        // 异步回来时可能已经切走了 tab
      load();
    }).catch(function (e) {
      if (!host.isConnected) return;
      btnEl.disabled = false;
      btnEl.textContent = '删除';
      var s = e && e.status;
      if (s === 503) { alert('删除失败：记忆库正在整理，过一会儿再试。'); }
      else if (s === 404) { alert('这条已经不在库里了，刷新一下。'); load(); }
      else { alert('删除失败：' + ((e && e.message) || '未知错误')); }
    });
  }

  /* ---------- ④ 反思日志 ---------- */
  function paintLogs(logs, busy) {
    logBox.innerHTML = '';
    if (!logs.length) {
      logBox.appendChild(note(busy ? '记忆库正在整理，过一会儿再看。'
        : '还没有整理记录。她整理过记忆之后，这里会留下痕迹。'));
      return;
    }
    var list = document.createElement('div');
    list.className = 'mem-log-list';
    logs.forEach(function (lg) {
      var line = document.createElement('div');
      line.className = 'mem-log-row';
      var top = document.createElement('div');
      top.className = 'mem-log-row__top';
      var t = document.createElement('span');
      t.className = 'mem-tag';
      t.textContent = LOG_TYPE[lg.type] || String(lg.type || '记录');
      top.appendChild(t);
      var time = document.createElement('span');
      time.className = 'mem-log-row__time';
      time.textContent = fmtTime(lg.createdAt);
      top.appendChild(time);
      line.appendChild(top);
      var sum = document.createElement('div');
      sum.className = 'mem-log-row__text';
      sum.textContent = String(lg.summary || '') || '（没有摘要）';
      line.appendChild(sum);
      list.appendChild(line);
    });
    logBox.appendChild(list);
  }

  /* ---------- 一次读全 ---------- */
  /* 骨架屏先占位：回来之前留白，看着像「本来就没有」。 */
  function load() {
    [statBox, profBox, l2Box, logBox].forEach(function (b) {
      b.innerHTML = '';
      b.appendChild(skeletonEl(2));
    });
    api('/memory/panel').then(function (d) {
      /* 守卫：读的过程中用户可能已经切走 tab（renderSettingsPanel 清空了 settingsBody），
         那时这几个盒子已脱离文档，再往里写是白写，重则碰到 null。 */
      if (!host.isConnected) return;
      paintStatus(d.status || {});
      paintProfile(d.l0 || {}, d.l1 || {});
      paintL2(d);
      paintLogs(d.reflectionLogs || [], d.l2Busy === true);
    }).catch(function (e) {
      if (!host.isConnected) return;
      failBox(statBox, '读不到记忆状态（服务可能正在重启）'
        + (e && e.status ? '：HTTP ' + e.status : ''));
      failBox(profBox, '画像与近况也没读到。', 'alert--info');
      failBox(l2Box, '长期条目的状态未知，先不列出来，免得看成空库。', 'alert--info');
      failBox(logBox, '反思日志同样没读到。', 'alert--info');
    });
  }

  load();
};

PANELS.tts = function (host, S) {
  var t = S.tts || {};
  var eng = t.engine === 'minimax' ? 'minimax' : (t.engine === 'custom' ? 'custom' : 'system');

  /* 引擎选择：手机自带 / MiniMax / 自定义云端 */
  var ce = card();
  ce.appendChild(row('朗读引擎',
    eng === 'minimax' ? 'MiniMax 云端合成，音色和桌面端同一个'
      : eng === 'custom' ? '你自己的云端接口，按下面的约定返回音频'
        : '手机自带的 termux-tts-speak，离线、免费',
    segmented(eng, [
      { value: 'system', label: '手机自带' },
      { value: 'minimax', label: 'MiniMax' },
      { value: 'custom', label: '自定义云端' }
    ], function (v) {
      /* 切引擎必须重画：三条嗓子的字段不一样，只存不画会看着像没反应 */
      saveSettings({ tts: { engine: v } }, true).then(function () { renderSettingsPanel(); });
    }),
    { stack: true }));

  if (eng === 'minimax') {
    ce.appendChild(row('API Key', '你自己的 MiniMax Key，只存在这台手机里',
      textInput(t.minimaxKey || '', function (v) { saveSettings({ tts: { minimaxKey: v } }); },
        { password: true, placeholder: 'sk-api-…', label: 'MiniMax API Key' }), null));
    ce.appendChild(row('音色 ID', '桌面端用的那个，照抄过来就行',
      textInput(t.minimaxVoiceId || '', function (v) { saveSettings({ tts: { minimaxVoiceId: v } }); },
        { mono: true, placeholder: 'cyrene-voice-…', label: '音色 ID' }), null));
    ce.appendChild(row('模型', '默认 speech-2.8-hd',
      textInput(t.minimaxModel || 'speech-2.8-hd',
        function (v) { saveSettings({ tts: { minimaxModel: v } }); },
        { mono: true, label: '模型' }), null));
    ce.appendChild(row('语速', 'MiniMax 的 speed，1.0 为正常',
      slider(0.5, 2.0, 0.1, t.minimaxSpeed != null ? t.minimaxSpeed : 1.0,
        function (v) { saveSettings({ tts: { minimaxSpeed: v } }); },
        function (v) { return v.toFixed(1) + '×'; }), { stack: true }));
    ce.appendChild(row('音量', 'MiniMax 的 vol，1.0 为正常',
      slider(0.1, 3.0, 0.1, t.minimaxVolume != null ? t.minimaxVolume : 1.0,
        function (v) { saveSettings({ tts: { minimaxVolume: v } }); },
        function (v) { return v.toFixed(1); }), { stack: true }));
    ce.appendChild(row('音调', '基频用的是模型自带的；慢速时嫌闷就往右拉一点',
      slider(-6, 6, 1, t.minimaxPitch != null ? t.minimaxPitch : 0,
        function (v) { saveSettings({ tts: { minimaxPitch: v } }); },
        function (v) { return (v > 0 ? '+' : '') + v; }), { stack: true }));
    ce.appendChild(row('气口增强', '话里带上哈/嗯/啊这些字时，补一个停顿标记；最多两处',
      toggle(t.minimaxVocalEnhance !== false,
        function (v) { saveSettings({ tts: { minimaxVocalEnhance: v } }, true); },
        '气口增强'), null));
  }

  if (eng === 'custom') {
    ce.appendChild(row('接口地址', '往这里 POST 一个 JSON，把音频拿回来',
      textInput(t.customEndpointUrl || '', function (v) { saveSettings({ tts: { customEndpointUrl: v } }); },
        { mono: true, placeholder: 'http://…/tts', label: '接口地址' }), null));
    ce.appendChild(row('API Key', '可以留空；填了就放进 Authorization: Bearer',
      textInput(t.customApiKey || '', function (v) { saveSettings({ tts: { customApiKey: v } }); },
        { password: true, label: '接口 Key' }), null));
    ce.appendChild(row('音色 ID', '可以留空，会作为 voiceId 发过去',
      textInput(t.customVoiceId || '', function (v) { saveSettings({ tts: { customVoiceId: v } }); },
        { mono: true, label: '音色 ID' }), null));
    ce.appendChild(row('返回格式', '问接口要 mp3 还是 wav',
      segmented(t.customFormat === 'wav' ? 'wav' : 'mp3',
        [{ value: 'mp3', label: 'mp3' }, { value: 'wav', label: 'wav' }],
        function (v) { saveSettings({ tts: { customFormat: v } }, true); }), { stack: true }));
    ce.appendChild(row('语速', '作为 speed 发过去，1.0 为正常',
      slider(0.5, 2.0, 0.1, t.customSpeed != null ? t.customSpeed : 1.0,
        function (v) { saveSettings({ tts: { customSpeed: v } }); },
        function (v) { return v.toFixed(1) + '×'; }), { stack: true }));
    ce.appendChild(row('音量', '作为 volume 发过去，1.0 为正常',
      slider(0.1, 3.0, 0.1, t.customVolume != null ? t.customVolume : 1.0,
        function (v) { saveSettings({ tts: { customVolume: v } }); },
        function (v) { return v.toFixed(1); }), { stack: true }));
    ce.appendChild(row('超时', '多少秒还没回就当失败',
      slider(5, 120, 5, t.customTimeoutMs != null ? Math.round(t.customTimeoutMs / 1000) : 30,
        function (v) { saveSettings({ tts: { customTimeoutMs: v * 1000 } }); },
        function (v) { return v + 's'; }), { stack: true }));
    var hint = document.createElement('div');
    hint.className = 'cy-settings-general__notice';
    hint.style.cssText = 'padding:4px 14px 12px';
    hint.textContent = '接口收到 {text, voiceId, speed, volume, format}；'
      + '直接吐音频字节，或者回 JSON 带一个 audioBase64 都行。';
    ce.appendChild(hint);
  }

  if (eng !== 'system') {
    ce.appendChild(row('说话的地方', '音频拿到后交给 termux-media-player 播',
      null, { notice: '要装 Termux:API' }));
  }
  host.appendChild(section('朗读引擎', null, ce));

  /* 自动朗读对所有引擎都生效；下面三条只有手机自带那条嗓子用得上 */
  var c = card();
  c.appendChild(row('自动朗读回复', '昔涟回完话就念出来',
    toggle(t.autoSpeak, function (v) { saveSettings({ tts: { autoSpeak: v } }, true); },
      '自动朗读回复'), null));
  if (eng === 'system') {
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
  }
  host.appendChild(section('语音朗读', eng === 'system'
    ? '依赖 Termux:API 的 termux-tts-speak；电脑上没有这个命令，播放会静默失败'
    : '语速/音调/语言是「手机自带」那条嗓子的参数，上面切到云端时已经收起来了', c));

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

/* ================= 版本与更新 ================= */
/* 检测和执行都调仓库根的 update.sh，网页和脚本共用一套判断，不会各说各话。
   执行时脚本会重启服务，所以页面短暂断连属正常。 */
var UPDATE_UI = { phase: 'idle', info: null, error: '', status: null, timer: 0 };

function updateCheck() {
  if (UPDATE_UI.phase === 'checking' || UPDATE_UI.phase === 'updating') return;
  UPDATE_UI.phase = 'checking'; UPDATE_UI.error = ''; UPDATE_UI.info = null;
  renderSettingsPanel();
  api('/update/check').then(function (d) {
    if (d && d.ok) { UPDATE_UI.info = d; UPDATE_UI.phase = 'ready'; }
    else { UPDATE_UI.error = (d && d.error) || '检测失败'; UPDATE_UI.phase = 'idle'; }
    renderSettingsPanel();
  }).catch(function (e) {
    UPDATE_UI.error = '检测失败：' + (e.message || e); UPDATE_UI.phase = 'idle';
    renderSettingsPanel();
  });
}

function updateApply() {
  if (UPDATE_UI.phase === 'updating') return;
  if (!confirm('现在更新到仓库最新版？\n\n会先下载新代码做语法自检，'
    + '更新前自动备份旧代码，完成后服务自动重启（网页断开一下再恢复）。')) return;
  UPDATE_UI.phase = 'updating'; UPDATE_UI.error = ''; UPDATE_UI.status = null;
  renderSettingsPanel();
  apiPost('/update/apply', { confirm: true }).then(function (d) {
    if (d && d.ok) { updatePoll(0); return; }
    UPDATE_UI.phase = 'idle';
    UPDATE_UI.error = (d && d.error) || '没能开始更新';
    renderSettingsPanel();
  }).catch(function (e) {
    UPDATE_UI.phase = 'idle';
    UPDATE_UI.error = '更新请求失败：' + (e.message || e);
    renderSettingsPanel();
  });
}

function updatePoll(n) {
  clearTimeout(UPDATE_UI.timer);
  var ticks = n || 0;
  api('/update/status').then(function (d) {
    UPDATE_UI.status = d;
    renderSettingsPanel();
    if (d && d.running) {
      UPDATE_UI.timer = setTimeout(function () { updatePoll(ticks + 1); }, 1500);
      return;
    }
    if (d && d.exitCode === 0) {
      UPDATE_UI.phase = 'done';           /* 换完代码了，服务正在被重启 */
    } else {
      UPDATE_UI.phase = 'idle';
      UPDATE_UI.error = '更新进程退出了（退出码 ' + (d && d.exitCode) + '），'
        + '看下面的输出，或者去 Termux 看 cyrene-web 的日志。';
    }
    renderSettingsPanel();
  }).catch(function () {
    /* 服务被重启时请求断掉是预期内的：继续等它回来 */
    ticks += 1;
    renderSettingsPanel();
    if (ticks < 200) {
      UPDATE_UI.timer = setTimeout(function () { updatePoll(ticks); }, 1500);
    } else {
      UPDATE_UI.phase = 'idle';
      UPDATE_UI.error = '等不到服务回来，去 Termux 看一眼 cyrene-web 还在不在。';
      renderSettingsPanel();
    }
  });
}

function updateLogText(t) {
  var ls = String(t || '').split('\n').filter(function (s) { return s.trim(); });
  return ls.slice(-10).join('\n');
}

function renderUpdateSection(host) {
  var c = card();
  var checking = UPDATE_UI.phase === 'checking';
  var updating = UPDATE_UI.phase === 'updating';
  var i = UPDATE_UI.info;

  c.appendChild(row('当前版本', checking
      ? '正在下载仓库快照并比对，第一次要十几 MB，稍等一下'
      : '和 GitHub 上 main 分支比代码指纹；只比对，不动文件',
    btn(checking ? '检测中…' : (i ? '重新检测' : '检测更新'),
      function () { updateCheck(); }, 'btn--ghost btn--sm'),
    { notice: (i && i.currentFingerprint)
        ? ('当前指纹 ' + i.currentFingerprint + ' · ' + i.currentCount + ' 个代码文件') : '' }));

  if (UPDATE_UI.error) {
    var e1 = document.createElement('div');
    e1.className = 'alert alert--err';
    e1.textContent = UPDATE_UI.error;
    c.appendChild(e1);
  }

  if (i && i.ok && !updating) {
    var box = document.createElement('div');
    box.className = 'alert alert--' + (i.hasUpdate ? 'warn' : 'ok');
    box.textContent = (i.hasUpdate
      ? ('仓库里有新版本：' + i.remoteCount + ' 个代码文件，指纹 ' + i.remoteFingerprint)
      : ('已经是最新版了（' + i.currentCount + ' 个代码文件，指纹 ' + i.currentFingerprint + '）'))
      + (i.source ? '　·　比对源 ' + i.source : '');
    c.appendChild(box);
  }

  if (i && i.ok && i.hasUpdate && !updating && UPDATE_UI.phase !== 'done') {
    var acts = document.createElement('div');
    acts.style.cssText = 'display:flex;gap:8px;padding:12px 14px 0';
    acts.appendChild(btn('立即更新', function () { updateApply(); }, 'btn--primary btn--sm'));
    c.appendChild(acts);
  }

  if (updating || UPDATE_UI.phase === 'done') {
    var tip = document.createElement('div');
    tip.className = 'alert alert--info';
    tip.textContent = UPDATE_UI.phase === 'done'
      ? '更新完成，服务正在重启 —— 页面稍后自己会好，一直转圈就手动刷一下。'
      : '正在更新：下载 → 语法自检 → 备份 → 换代码 → 重启服务。中途网页会断一下，属正常。';
    c.appendChild(tip);
    var pre = document.createElement('pre');
    pre.style.cssText = 'margin:10px 14px 0;padding:10px;overflow:auto;max-height:180px;'
      + 'font-size:12px;line-height:1.5;white-space:pre-wrap;'
      + 'background:rgba(127,127,127,.12);border-radius:8px';
    pre.textContent = updateLogText(UPDATE_UI.status && UPDATE_UI.status.tail) || '（等输出…）';
    c.appendChild(pre);
  }

  if (i && i.ok && i.tail && !updating && UPDATE_UI.phase !== 'done') {
    var det = document.createElement('details');
    det.style.cssText = 'margin:10px 14px 0';
    var sm = document.createElement('summary');
    sm.textContent = 'update.sh 的原始输出';
    det.appendChild(sm);
    var pre2 = document.createElement('pre');
    pre2.style.cssText = 'margin:8px 0 0;padding:10px;overflow:auto;max-height:200px;'
      + 'font-size:12px;line-height:1.5;white-space:pre-wrap;'
      + 'background:rgba(127,127,127,.12);border-radius:8px';
    pre2.textContent = i.tail;
    det.appendChild(pre2);
    c.appendChild(det);
  }

  if (i && i.ok && i.note) {
    var nb = document.createElement('div');
    nb.className = 'cy-settings-general__notice';
    nb.style.cssText = 'padding:6px 14px 0';
    nb.textContent = i.note;
    c.appendChild(nb);
  }

  if (i && i.ok && i.changedCount) {
    var det2 = document.createElement('details');
    det2.style.cssText = 'margin:10px 14px 0';
    var sm2 = document.createElement('summary');
    sm2.textContent = '有变化的文件（' + i.changedCount + ' 个）';
    det2.appendChild(sm2);
    var pre3 = document.createElement('pre');
    pre3.style.cssText = 'margin:8px 0 0;padding:10px;overflow:auto;max-height:200px;'
      + 'font-size:12px;line-height:1.5;white-space:pre-wrap;'
      + 'background:rgba(127,127,127,.12);border-radius:8px';
    pre3.textContent = (i.changed || []).join('\n');
    det2.appendChild(pre3);
    c.appendChild(det2);
  }

  host.appendChild(section('版本与更新', '检测仓库有没有新版，有就一键更新', c));
}

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
  var info = row('版本', 'cyrene_web.py v9 · marked v15.0.12 · highlight.js v11.11.1', null);
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

  renderUpdateSection(host);
};

/* ---------- 模式：默认模式 + 各模式提示词规模 ---------- */
PANELS.mode = function (host, S) {
  var chat = S.chat || {};
  var info = S.info || {};
  var byMode = info.prompt_chars_by_mode || {};
  var cur = chat.defaultMode || 'chat';
  var list = (MODES && MODES.length) ? MODES : [
    { id: 'chat', label: '聊天', icon: 'chat', tools: false },
    { id: 'work', label: '工作', icon: 'work', tools: true },
    { id: 'code', label: '代码', icon: 'code', tools: true },
    { id: 'learn', label: '学习', icon: 'learn', tools: true }
  ];

  /* 默认模式卡片列表，选中项高亮 */
  var grid = document.createElement('div'); grid.className = 'mode-grid';
  list.forEach(function (m) {
    var active = m.id === cur;
    var b = document.createElement('button');
    b.type = 'button';
    b.className = 'mode-card' + (active ? ' is-active' : '');
    b.setAttribute('aria-pressed', String(active));
    /* 用 DOM 构造而不是 innerHTML：label / desc 来自后端配置，
       拼字符串会把它们当 HTML 解析。图标同理只作 attribute 值。 */
    var mcIcon = document.createElement('span');
    mcIcon.className = 'mode-card__icon';
    mcIcon.setAttribute('aria-hidden', 'true');
    renderBackendIcon(mcIcon, m.icon || m.id, 18);
    var mcLabel = document.createElement('span');
    mcLabel.className = 'mode-card__label';
    mcLabel.textContent = m.label || m.id;
    var mcTick = document.createElement('span');
    mcTick.className = 'mode-card__tick';
    mcTick.setAttribute('aria-hidden', 'true');
    setIcon(mcTick, 'check', 16);
    mcLabel.appendChild(mcTick);
    var mcDesc = document.createElement('span');
    mcDesc.className = 'mode-card__desc';
    mcDesc.textContent = m.desc || '';
    var mcMeta = document.createElement('span');
    mcMeta.className = 'mode-card__meta';
    mcMeta.textContent = (m.tools ? '可用工具' : '无工具')
      + (byMode[m.id] ? ' · ' + Number(byMode[m.id]).toLocaleString('en-US') + ' 字' : '');
    b.appendChild(mcIcon); b.appendChild(mcLabel);
    b.appendChild(mcDesc); b.appendChild(mcMeta);
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
    c.appendChild(row(m.label || m.id,
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

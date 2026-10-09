"use strict";
/**
 * plugin_host.cjs —— 手机端 Cyrene 的 Node 插件宿主壳（P1-3）
 *
 * 契约基准：桌面端 Cyrene Plugin API v1
 *   （Playa-Cyrene/Cyrene-Agent · packages/plugin-sdk/src/api.ts）
 * 目标：让市场仓库里的 index.cjs 插件**不改一行代码**就能在手机上跑。
 *
 * 进程模型：一个插件一个 node 子进程，由 Python 宿主（cyrene_web.py 的
 * BridgeClient）spawn。崩溃隔离 + 独立超时 + 可单独 kill 做停用。
 *
 * 传输：NDJSON over stdio（一行一个 JSON-RPC 2.0 对象），双向。
 *   Python → Node：plugin.register / tool.execute / prompt.provide /
 *                  plugin.dispose / host.event
 *   Node → Python：storage.* / secrets.* / llm.* / conversations.* /
 *                  events.* / log
 *
 * ⚠ 生死线：stdout 只准出现协议 JSON 行。插件里任何 console.log 都会
 *   写 stdout，一旦出现半行文本，Python 侧 json.loads 就炸，整个插件
 *   表现为「莫名崩溃」。所以本文件第一件事就是把 console.* 全部改道
 *   到 stderr（见下方 installConsoleRedirect），再由 Python 转宿主日志。
 *
 * ⚠ 并发：Node 收到 tool.execute 后，插件内部可能回头调 storage.get，
 *   形成**交错的嵌套请求**。所以绝不能「发一个等一个」串行处理，
 *   必须允许 in-flight 请求并存（用 Map 存 pending，按 id 配对）。
 */

const path = require("path");
const readline = require("readline");

// ---------------------------------------------------------------------------
// 常量
// ---------------------------------------------------------------------------

/** 桌面端 CURRENT_PLUGIN_API_VERSION = 1。不匹配则拒绝加载。 */
const SUPPORTED_API_VERSION = 1;

/**
 * 宿主服务能力清单（对齐 SDK 的 PLUGIN_CAPABILITIES）。
 * 手机端的实现程度分三档：
 *   ok      —— Python 侧已实现，正常投影
 *   stub    —— 按契约返回固定空值（workspace.getBinding → null,
 *              channels.has → false），这是契约允许的可空返回，不算失败
 *   absent  —— 手机端没有这个子系统，按契约抛带 code 的错误
 *              （SDK 明确：插件靠 isPluginHostError(e.code) 分支，
 *                静默返回 undefined 会让插件走进错误路径）
 */
const CAPABILITY_STATUS = {
  channels: "stub",
  llm: "ok",
  secrets: "ok",
  workspace: "stub",
  conversations: "ok",
  scheduler: "absent",
  "speech-input": "absent",
};

/** 对齐 SDK 的 PLUGIN_HOST_ERROR_CODES（九种，一个不多一个不少）。 */
const HOST_ERROR_CODES = new Set([
  "E_CAPABILITY_UNAVAILABLE",
  "E_INVALID_ARGUMENT",
  "E_NOT_FOUND",
  "E_NOT_OWNER",
  "E_STORAGE_UNAVAILABLE",
  "E_SPEECH_INPUT_BUSY",
  "E_NO_ACTIVE_INPUT_TARGET",
  "E_PLUGIN_STOPPING",
  "E_INTERNAL",
]);

/** 宿主事件命名空间前缀。插件自有事件由框架补 plugin:<id>: 前缀。 */
const HOST_EVENT_PREFIX = "host:";
const PLUGIN_EVENT_PREFIX = "plugin:";

// ---------------------------------------------------------------------------
// stdout / console 纪律
// ---------------------------------------------------------------------------

/**
 * 协议输出走这里。JSON.stringify 后补 \n，一次性 write。
 * 分两次 write（内容 + 换行）会让 Python 侧读到半行，绝不能那么干。
 */
function send(obj) {
  try {
    process.stdout.write(JSON.stringify(obj) + "\n");
  } catch (e) {
    // 连序列化都失败（循环引用等），只能退到 stderr 报告，
    // 此时协议流已不可信，Python 侧会因超时判定失败。
    process.stderr.write(`[plugin_host] send 失败: ${e && e.message}\n`);
  }
}

/**
 * 把 console.* 改道 stderr，保住 stdout 的协议纯净。
 * 必须在 require 插件之前执行 —— 插件顶层就可能在打日志。
 */
function installConsoleRedirect(tag) {
  const toStderr = (level) => (...args) => {
    const parts = args.map((a) => {
      if (typeof a === "string") return a;
      try {
        return JSON.stringify(a);
      } catch {
        return String(a);
      }
    });
    process.stderr.write(`[${tag}][${level}] ${parts.join(" ")}\n`);
  };
  console.log = toStderr("log");
  console.info = toStderr("info");
  console.debug = toStderr("debug");
  console.warn = toStderr("warn");
  // console.error 也改道：它本来就写 stderr，但要统一加前缀方便宿主归类
  console.error = toStderr("error");
}

// ---------------------------------------------------------------------------
// 错误构造：让插件的 isPluginHostError(e) 判定成立
// ---------------------------------------------------------------------------

/**
 * 造一个带 code 的 Error。SDK 的 isPluginHostError 判定条件是
 * `value instanceof Error && PLUGIN_HOST_ERROR_CODES.has(value.code)`，
 * 所以必须是真的 Error 实例且 code 在九种之内。
 */
function hostError(code, message) {
  const safeCode = HOST_ERROR_CODES.has(code) ? code : "E_INTERNAL";
  const e = new Error(message || safeCode);
  e.code = safeCode;
  e.name = "PluginHostError";
  return e;
}

/** 能力不可用的标准错误。absent 档的 deps 全走这里。 */
function capabilityUnavailable(cap, method) {
  return hostError(
    "E_CAPABILITY_UNAVAILABLE",
    `宿主能力 "${cap}" 在手机端不可用（调用 ${cap}.${method}）。` +
      `手机端未实现该子系统。`
  );
}

// ---------------------------------------------------------------------------
// 双向 RPC
// ---------------------------------------------------------------------------

let nextRpcId = 1;
/** id → {resolve, reject}。允许 in-flight 并存（交错嵌套请求）。 */
const pending = new Map();

/**
 * 向 Python 发请求并等响应。
 * @param {string} method
 * @param {object} params
 * @param {number} timeoutMs 0 = 不超时（由 Python 侧兜底）
 */
function rpc(method, params, timeoutMs = 0) {
  return new Promise((resolve, reject) => {
    const id = nextRpcId++;
    let timer = null;
    pending.set(id, {
      resolve: (v) => {
        if (timer) clearTimeout(timer);
        pending.delete(id);
        resolve(v);
      },
      reject: (e) => {
        if (timer) clearTimeout(timer);
        pending.delete(id);
        reject(e);
      },
    });
    if (timeoutMs > 0) {
      timer = setTimeout(() => {
        const p = pending.get(id);
        if (p) p.reject(hostError("E_INTERNAL", `RPC ${method} 超时 ${timeoutMs}ms`));
      }, timeoutMs);
    }
    send({ jsonrpc: "2.0", id, method, params: params || {} });
  });
}

/** 单向通知（不等响应）。log 走这条，避免日志刷爆时阻塞。 */
function notify(method, params) {
  send({ jsonrpc: "2.0", method, params: params || {} });
}

/** Python 的响应回来了，按 id 配对。error 分支还原成带 code 的 Error。 */
function handleResponse(msg) {
  const p = pending.get(msg.id);
  if (!p) return; // 超时后迟到的响应，丢弃
  if (msg.error) {
    p.reject(hostError(msg.error.code || "E_INTERNAL", msg.error.message));
  } else {
    p.resolve(msg.result);
  }
}

// ---------------------------------------------------------------------------
// 插件上下文（PluginContext）
// ---------------------------------------------------------------------------

/**
 * registerTool / registerPromptProvider 在 Node 侧**只往本地数组塞**，
 * 不逐个 RPC —— 否则注册 5 个工具就是 5 次进程间往返，纯属浪费。
 * plugin.register 的响应里一次性把清单带回 Python。
 */
let registeredTools = [];
let registeredProviders = [];
let registeredIpc = [];

/** onDispose 的清理回调栈；契约要求逆序执行且每个最多一次。 */
let disposeStack = [];
let disposed = false;

/** 插件停止信号。dispose / 停用前先 abort，让插件有机会收尾。 */
const abortController = new AbortController();

/** 宿主事件订阅表：event → Set<listener>。on() 返回退订函数。 */
const eventSubs = new Map();

function buildStorage(pluginId, storageRoot) {
  return {
    get(key) {
      if (typeof key !== "string" || !key) {
        throw hostError("E_INVALID_ARGUMENT", "storage.get 需要非空字符串 key");
      }
      // storage 是同步契约（get<T>(key): T | undefined），但 Python 侧是
      // 进程外的，只能异步。这里用 deasync 会引入原生依赖（手机上装不动），
      // 所以改为：register 阶段 Python 预先把整个 kv 快照塞进来，
      // get 直接读本地缓存，set 异步回写（fire-and-forget + 记录待写集）。
      // 契约语义得以保持：插件看到的 get 是同步的。
      return storageCache.has(key) ? storageCache.get(key) : undefined;
    },
    set(key, value) {
      if (typeof key !== "string" || !key) {
        throw hostError("E_INVALID_ARGUMENT", "storage.set 需要非空字符串 key");
      }
      storageCache.set(key, value);
      dirtyKeys.add(key);
      // 立即异步回写，保证宿主崩溃时数据不丢太久。
      // 不 await：契约里 set 是 void，插件不会等。
      // 写成功后把自己从 dirtyKeys 摘掉，并把 Promise 从 pendingWrites 移除，
      // 这样 doDispose 的 flush 只兜「确实还没落地」的 key，不会重复写。
      const p = rpc("storage.set", { key, value })
        .then(() => {
          dirtyKeys.delete(key);
        })
        .catch((e) => {
          process.stderr.write(`[plugin_host] storage.set(${key}) 失败: ${e.message}\n`);
        })
        .finally(() => {
          pendingWrites.delete(p);
        });
      pendingWrites.add(p);
    },
    rootDir() {
      return storageRoot;
    },
  };
}

/** storage 本地缓存（register 前由 Python 灌入快照）。 */
let storageCache = new Map();
/** 待写回的 key 集合，dispose 时做最后一次 flush。 */
let dirtyKeys = new Set();
/**
 * 在途的写回 Promise 集合。
 *
 * 为什么需要它：set() 是 fire-and-forget（契约里 set 返回 void，插件不会等），
 * 而 doDispose 的 flush 紧跟在 onDispose 回调之后执行。插件在 onDispose 里
 * 调 storage.set 时，那次写在途还没落地，dirtyKeys 里仍有该 key —— flush 会
 * 再写一遍（本轮自测实测到 storage.set 发了两次）。
 * 光靠「写成功后 delete key」消不掉这个竞态，因为 delete 发生在 flush 之后。
 * 正确做法：flush 先 await 全部在途写，再看 dirtyKeys 还剩什么。
 */
let pendingWrites = new Set();

function buildEvents(pluginId) {
  return {
    on(event, listener) {
      if (typeof event !== "string" || typeof listener !== "function") {
        throw hostError("E_INVALID_ARGUMENT", "events.on 需要 (string, function)");
      }
      if (!eventSubs.has(event)) eventSubs.set(event, new Set());
      eventSubs.get(event).add(listener);
      // 契约：返回退订函数
      return () => {
        const set = eventSubs.get(event);
        if (set) {
          set.delete(listener);
          if (set.size === 0) eventSubs.delete(event);
        }
      };
    },
    async emit(event, payload) {
      if (typeof event !== "string" || !event) {
        throw hostError("E_INVALID_ARGUMENT", "events.emit 需要非空字符串 event");
      }
      // 框架自动补全为 plugin:<id>:<event>
      const full = event.startsWith(HOST_EVENT_PREFIX) || event.startsWith(PLUGIN_EVENT_PREFIX)
        ? event
        : `${PLUGIN_EVENT_PREFIX}${pluginId}:${event}`;
      await rpc("events.emit", { event: full, payload: payload === undefined ? null : payload });
    },
  };
}

/** 把 event 派发给本地订阅者。listener 抛错不能带走宿主，逐个 try。 */
function dispatchLocalEvent(event, payload) {
  const set = eventSubs.get(event);
  if (!set || set.size === 0) return;
  for (const fn of Array.from(set)) {
    try {
      const r = fn(payload);
      if (r && typeof r.then === "function") {
        r.catch((e) => {
          process.stderr.write(`[plugin_host] 事件 ${event} 监听器异常: ${e && e.message}\n`);
        });
      }
    } catch (e) {
      process.stderr.write(`[plugin_host] 事件 ${event} 监听器异常: ${e && e.message}\n`);
    }
  }
}

/**
 * deps 投影。按 CAPABILITY_STATUS 分档：
 *   ok     → RPC 到 Python
 *   stub   → 契约允许的固定空值
 *   absent → 抛 E_CAPABILITY_UNAVAILABLE（不静默）
 * 只有 manifest.deps 声明过的能力才注入，未声明的保持 undefined
 * （SDK 语义：deps 是服务可用性声明，未声明不注入）。
 */
function buildDeps(pluginId, declared) {
  const want = new Set(Array.isArray(declared) ? declared : []);
  const deps = {};

  // channels: stub —— 手机端没有渠道系统，has() 恒 false。
  // 这是只读发现接口，返回 false 完全符合契约，不需要报错。
  if (want.has("channels")) {
    deps.channels = { has: () => false };
  }

  // llm: ok
  if (want.has("llm")) {
    deps.llm = {
      generateText(messages, options) {
        if (!Array.isArray(messages) || messages.length === 0) {
          return Promise.reject(
            hostError("E_INVALID_ARGUMENT", "llm.generateText 需要非空 messages 数组")
          );
        }
        return rpc("llm.generateText", { messages, options: options || {} }, 300000);
      },
      // runGoal 是「较新宿主提供的无头目标循环」，SDK 明确老宿主没有该方法，
      // 插件应保留兼容回退。手机端不提供 —— 但**不能定义为返回错误的函数**，
      // 否则插件的 `if (deps.llm.runGoal)` 探测会误判为可用。
      // 契约是可选方法，所以直接不挂这个键。
    };
  }

  // secrets: ok。命名空间由 Python 侧按 pluginId 隔离，Node 侧不传 id。
  if (want.has("secrets")) {
    deps.secrets = {
      get(key) {
        if (typeof key !== "string" || !key) {
          return Promise.reject(hostError("E_INVALID_ARGUMENT", "secrets.get 需要非空 key"));
        }
        return rpc("secrets.get", { key });
      },
      set(key, value) {
        if (typeof key !== "string" || !key) {
          return Promise.reject(hostError("E_INVALID_ARGUMENT", "secrets.set 需要非空 key"));
        }
        return rpc("secrets.set", { key, value: String(value) });
      },
      delete(key) {
        if (typeof key !== "string" || !key) {
          return Promise.reject(hostError("E_INVALID_ARGUMENT", "secrets.delete 需要非空 key"));
        }
        return rpc("secrets.delete", { key });
      },
    };
  }

  // workspace: stub —— 手机端无工作区绑定概念。
  // 契约签名是 Promise<PluginWorkspaceBinding | null>，返回 null 合法。
  if (want.has("workspace")) {
    deps.workspace = { getBinding: async () => null };
  }

  // conversations: ok
  if (want.has("conversations")) {
    deps.conversations = {
      list(input) {
        return rpc("conversations.list", { input: input || {} });
      },
      getMessages(input) {
        if (!input || typeof input.conversationId !== "string") {
          return Promise.reject(
            hostError("E_INVALID_ARGUMENT", "conversations.getMessages 需要 conversationId")
          );
        }
        return rpc("conversations.getMessages", { input });
      },
    };
  }

  // scheduler: absent —— 手机端无定时任务子系统。
  // 必须挂上对象再让每个方法抛错，因为插件可能先探测 deps.scheduler 存在性。
  // 挂 undefined 会让 `deps.scheduler?.createTask(...)` 静默变 undefined，
  // 插件以为创建成功了 —— 那是最坏的结果。
  if (want.has("scheduler")) {
    const throwIt = (m) => () => Promise.reject(capabilityUnavailable("scheduler", m));
    deps.scheduler = {
      createTask: throwIt("createTask"),
      listTasks: throwIt("listTasks"),
      updateTask: throwIt("updateTask"),
      deleteTask: throwIt("deleteTask"),
      getHistory: throwIt("getHistory"),
    };
  }

  // speech-input: absent —— 手机端无语音输入租约机制。
  if (want.has("speech-input")) {
    deps.speechInput = {
      acquire: () => Promise.reject(capabilityUnavailable("speech-input", "acquire")),
    };
  }

  return deps;
}

/**
 * 构造 PluginContext。字段严格对齐 SDK 的 interface PluginContext。
 */
function buildContext(pluginId, manifest, storageRoot) {
  const ctx = {
    id: pluginId,
    // 契约：readonly signal，插件停止或激活回滚开始前会先触发取消
    signal: abortController.signal,

    onDispose(cleanup) {
      if (typeof cleanup !== "function") {
        throw hostError("E_INVALID_ARGUMENT", "onDispose 需要函数");
      }
      if (disposed) {
        // 已经在 dispose 流程里了，再登记也没机会执行 —— 明确报错，
        // 静默忽略会让插件以为清理逻辑已挂载。
        throw hostError("E_PLUGIN_STOPPING", "插件已在停止流程中，无法再登记 onDispose");
      }
      disposeStack.push(cleanup);
    },

    events: buildEvents(pluginId),

    registerTool(tool) {
      if (!tool || typeof tool !== "object") {
        throw hostError("E_INVALID_ARGUMENT", "registerTool 需要 PluginTool 对象");
      }
      if (typeof tool.id !== "string" || !tool.id) {
        throw hostError("E_INVALID_ARGUMENT", "registerTool: tool.id 必填且为字符串");
      }
      if (typeof tool.execute !== "function") {
        throw hostError("E_INVALID_ARGUMENT", `registerTool(${tool.id}): execute 必须是函数`);
      }
      // 同 id 覆盖（与桌面端一致：重复注册以最后一次为准）
      registeredTools = registeredTools.filter((t) => t.id !== tool.id);
      registeredTools.push(tool);
    },

    unregisterTool(toolId) {
      registeredTools = registeredTools.filter((t) => t.id !== toolId);
    },

    registerPromptProvider(provider) {
      if (!provider || typeof provider !== "object") {
        throw hostError("E_INVALID_ARGUMENT", "registerPromptProvider 需要对象");
      }
      if (typeof provider.id !== "string" || !provider.id) {
        throw hostError("E_INVALID_ARGUMENT", "registerPromptProvider: provider.id 必填");
      }
      if (typeof provider.provide !== "function") {
        throw hostError(
          "E_INVALID_ARGUMENT",
          `registerPromptProvider(${provider.id}): provide 必须是函数`
        );
      }
      registeredProviders = registeredProviders.filter((p) => p.id !== provider.id);
      registeredProviders.push(provider);
    },

    unregisterPromptProvider(providerId) {
      registeredProviders = registeredProviders.filter((p) => p.id !== providerId);
    },

    registerIpc(channel, handler) {
      if (typeof channel !== "string" || typeof handler !== "function") {
        throw hostError("E_INVALID_ARGUMENT", "registerIpc 需要 (string, function)");
      }
      // 自动命名空间化为 plugin:<id>:<channel>
      const full = `${PLUGIN_EVENT_PREFIX}${pluginId}:${channel}`;
      registeredIpc = registeredIpc.filter((c) => c.channel !== full);
      registeredIpc.push({ channel: full, handler });
    },

    unregisterIpc(channel) {
      const full = `${PLUGIN_EVENT_PREFIX}${pluginId}:${channel}`;
      registeredIpc = registeredIpc.filter((c) => c.channel !== full);
    },

    /**
     * registerChannelAdapter：手机端没有渠道系统。
     * 契约是 Promise<void>，这里明确拒绝而不是静默成功 ——
     * 插件以为渠道接上了却永远收不到消息，比直接失败更难排查。
     */
    registerChannelAdapter() {
      return Promise.reject(capabilityUnavailable("channels", "registerChannelAdapter"));
    },
    unregisterChannelAdapter() {
      return Promise.resolve();
    },

    storage: buildStorage(pluginId, storageRoot),
    deps: buildDeps(pluginId, manifest.deps),

    log(...args) {
      // 转宿主日志，前缀由 Python 侧加 [plugin:<id>]。
      // 走 notify（不等响应），日志不该阻塞插件。
      notify("log", {
        args: args.map((a) => (typeof a === "string" ? a : safeStringify(a))),
      });
    },
  };
  return ctx;
}

function safeStringify(v) {
  try {
    return JSON.stringify(v);
  } catch {
    return String(v);
  }
}

/**
 * 工具清单的**可序列化投影**：只回传 Python 需要的元数据，
 * 不含 execute（函数过不了 JSON）。Python 据此建 TOOLS 条目与 FC schema。
 */
function projectTool(t) {
  const schema = t.inputSchema || { type: "object", properties: {} };
  return {
    id: t.id,
    name: typeof t.name === "string" ? t.name : t.id,
    description: typeof t.description === "string" ? t.description : "",
    catalogHint: t.catalogHint || null,
    category: t.category || null,
    enabled: t.enabled !== false,
    risk: t.risk || "safe",
    modes: Array.isArray(t.modes) ? t.modes : null,
    needsContext: !!t.needsContext,
    effectKind: t.effectKind || null,
    deprecated: !!t.deprecated,
    inputSchema: {
      type: schema.type || "object",
      properties: schema.properties || {},
      required: Array.isArray(schema.required) ? schema.required : [],
    },
  };
}

function projectProvider(p) {
  return {
    id: p.id,
    modes: Array.isArray(p.modes) ? p.modes : null,
    sources: Array.isArray(p.sources) ? p.sources : null,
  };
}

// ---------------------------------------------------------------------------
// Python → Node 的方法处理
// ---------------------------------------------------------------------------

let PLUGIN = null; // 加载后的插件对象
let CTX = null;
let PLUGIN_ID = null;

/** plugin.register：加载插件、跑 register、一次性回传注册清单。 */
async function doRegister(params) {
  const manifest = params.manifest;
  if (!manifest || typeof manifest !== "object") {
    throw hostError("E_INVALID_ARGUMENT", "plugin.register 缺少 manifest");
  }

  // apiVersion 门禁：只支持 v1。桌面端 SDK 的 CURRENT_PLUGIN_API_VERSION = 1。
  if (manifest.apiVersion !== SUPPORTED_API_VERSION) {
    throw hostError(
      "E_INVALID_ARGUMENT",
      `插件要求 apiVersion=${manifest.apiVersion}，本宿主只支持 ${SUPPORTED_API_VERSION}`
    );
  }

  PLUGIN_ID = manifest.id;
  const pluginDir = params.pluginDir;
  const entryName = manifest.entry;
  if (!pluginDir || !entryName) {
    throw hostError("E_INVALID_ARGUMENT", "plugin.register 缺少 pluginDir 或 manifest.entry");
  }
  // entry 是「插件目录内的裸文件名」（契约原文），拼绝对路径时防穿越
  if (entryName.includes("/") || entryName.includes("\\") || entryName.includes("..")) {
    throw hostError("E_INVALID_ARGUMENT", `非法 entry（必须是裸文件名）: ${entryName}`);
  }
  const entryPath = path.resolve(pluginDir, entryName);
  if (!entryPath.startsWith(path.resolve(pluginDir))) {
    throw hostError("E_INVALID_ARGUMENT", `entry 越出插件目录: ${entryPath}`);
  }

  // storage 快照：Python 在 register 时灌入，让同步的 storage.get 能用
  storageCache = new Map();
  const snap = params.storageSnapshot;
  if (snap && typeof snap === "object") {
    for (const [k, v] of Object.entries(snap)) storageCache.set(k, v);
  }

  CTX = buildContext(PLUGIN_ID, manifest, params.storageRoot || path.join(pluginDir, "data"));

  // 加载插件模块。require 抛错就让它冒泡，Python 侧落 failed + 记 stderr。
  PLUGIN = require(entryPath);
  if (!PLUGIN || typeof PLUGIN.register !== "function") {
    throw hostError(
      "E_INVALID_ARGUMENT",
      `插件入口未导出 register(ctx) 函数（module.exports = { register }）。` +
        `实际导出: ${typeof PLUGIN}`
    );
  }

  // open() 是可选的早期钩子。它失败不该让整颗插件判死：
  //   · 桌面端大量插件的 open() 只负责「弹一个独立窗口」，手机上没有 Electron，
  //     这类失败与插件主体能力无关；
  //   · 还有些插件的 open() 依赖 register() 阶段才建立的上下文
  //     （cyrene-browser 的 open() 就用 pluginContext，而宿主是先 open 后 register，
  //      它必然拿到 null → 抛「插件尚未注册」，整颗判 failed）；
  //   · 契约里 open() 本来就是可选钩子，可选钩子不该有否决权。
  // 所以这里吞掉异常，只留一行 stderr；真正的失败交给 register() 去报。
  if (typeof PLUGIN.open === "function") {
    try {
      await PLUGIN.open();
    } catch (e) {
      process.stderr.write(
        `[plugin_host] open() 失败已忽略（可选钩子，不影响注册）: ${
          (e && e.message) || e
        }
`
      );
    }
  }

  await PLUGIN.register(CTX);

  return {
    ok: true,
    pluginId: PLUGIN_ID,
    tools: registeredTools.map(projectTool),
    promptProviders: registeredProviders.map(projectProvider),
    ipcChannels: registeredIpc.map((c) => c.channel),
    // 告诉 Python 哪些声明的能力其实是 absent，供面板明示
    capabilities: CAPABILITY_STATUS,
    warnings: collectWarnings(manifest),
  };
}

/** 声明了 absent 档能力的插件，注册成功但要给面板一条明示。 */
function collectWarnings(manifest) {
  const out = [];
  const want = Array.isArray(manifest.deps) ? manifest.deps : [];
  for (const cap of want) {
    if (CAPABILITY_STATUS[cap] === "absent") {
      out.push(`宿主能力 "${cap}" 在手机端不可用，调用会返回 E_CAPABILITY_UNAVAILABLE`);
    }
  }
  if (manifest.settingsPanel) {
    out.push("插件声明了 settingsPanel（面板 UI），P7 阶段才会挂载");
  }
  return out;
}

/** tool.execute：跑插件工具的 execute，返回字符串结果。 */
async function doToolExecute(params) {
  // disposed 必须在查表**之前**判：doDispose 末尾会清空 registeredTools，
  // 若先查表就会返回 E_NOT_FOUND（工具"从没存在过"）而不是 E_PLUGIN_STOPPING
  // （工具存在但宿主正在停）。两个 code 语义不同，插件的分支会走偏。
  if (disposed) {
    throw hostError("E_PLUGIN_STOPPING", "插件正在停止，拒绝执行工具");
  }
  const tool = registeredTools.find((t) => t.id === params.toolId);
  if (!tool) {
    throw hostError("E_NOT_FOUND", `工具未注册: ${params.toolId}`);
  }
  const args = params.args && typeof params.args === "object" ? params.args : {};
  // 契约签名是 execute(args, ctx?)。ctx 由 Python 侧按 needsContext 决定是否
  // 传值：不需要的工具传 undefined 进去也无害（插件自己不解构就不会读到），
  // 所以这里统一按两参调用，不在 Node 侧再分叉 —— 分叉只会让「插件声明了
  // needsContext 但宿主没传」这种配置错误被静默吞掉。
  const toolCtx = params.ctx && typeof params.ctx === "object" ? params.ctx : undefined;
  // 契约要求 execute 返回 Promise<string>。插件若返回非字符串，兜底转一下，
  // 因为 Python 侧要把它当工具结果塞回对话，非字符串会污染消息体。
  const raw = await tool.execute(args, toolCtx);
  if (typeof raw === "string") return { text: raw };
  if (raw === undefined || raw === null) return { text: "" };
  return { text: safeStringify(raw) };
}

/** prompt.provide：跑提示词 provider，返回注入文本。 */
async function doPromptProvide(params) {
  const prov = registeredProviders.find((p) => p.id === params.providerId);
  if (!prov) {
    throw hostError("E_NOT_FOUND", `提示词 provider 未注册: ${params.providerId}`);
  }
  if (disposed) {
    throw hostError("E_PLUGIN_STOPPING", "插件正在停止，拒绝构建提示词");
  }
  const input = Object.assign({}, params.input || {}, { signal: abortController.signal });
  const text = await prov.provide(input);
  return { text: typeof text === "string" ? text : text == null ? "" : safeStringify(text) };
}

/** ipc.invoke：调插件注册的 IPC handler（面板 UI 用，P7 才接前端）。 */
async function doIpcInvoke(params) {
  const hit = registeredIpc.find((c) => c.channel === params.channel);
  if (!hit) {
    throw hostError("E_NOT_FOUND", `IPC 通道未注册: ${params.channel}`);
  }
  const args = Array.isArray(params.args) ? params.args : [];
  const result = await hit.handler(...args);
  return { result: result === undefined ? null : result };
}

/**
 * plugin.dispose：停止插件。
 * 顺序对齐契约：先 abort signal（让插件知道要停了）→ 逆序跑 onDispose
 * 且每个最多一次 → 调 unregister() → flush 未写回的 storage。
 */
async function doDispose() {
  if (disposed) return { ok: true, alreadyDisposed: true };
  disposed = true;

  // 1. 先触发取消信号：契约「插件停止或激活回滚开始前会先触发取消」
  try {
    abortController.abort();
  } catch (e) {
    process.stderr.write(`[plugin_host] abort 失败: ${e && e.message}\n`);
  }

  // 2. 逆序执行清理回调，每个最多一次（用 swap-out 保证不重入）
  const stack = disposeStack;
  disposeStack = [];
  const errors = [];
  for (let i = stack.length - 1; i >= 0; i--) {
    try {
      await stack[i]();
    } catch (e) {
      errors.push(`onDispose[${i}]: ${e && e.message}`);
    }
  }

  // 3. 调插件的 unregister()
  if (PLUGIN && typeof PLUGIN.unregister === "function") {
    try {
      await PLUGIN.unregister();
    } catch (e) {
      errors.push(`unregister: ${e && e.message}`);
    }
  }

  // 4. flush 未写回的 storage key。
  //    必须先 await 在途写：onDispose 里的 storage.set 是 fire-and-forget，
  //    执行到这一步时那次 RPC 很可能还在飞。不先等就会看到 dirtyKeys 里
  //    仍有该 key，于是重复写一次（本轮自测实测到的 storage.set ×2）。
  if (pendingWrites.size > 0) {
    await Promise.allSettled(Array.from(pendingWrites));
  }
  // 到这里 dirtyKeys 剩下的才是真的没写成功的（RPC 失败或被拒），补一次同步等待。
  if (dirtyKeys.size > 0) {
    for (const key of Array.from(dirtyKeys)) {
      try {
        await rpc("storage.set", { key, value: storageCache.get(key) }, 5000);
        dirtyKeys.delete(key);
      } catch (e) {
        errors.push(`storage.set(${key}): ${e && e.message}`);
      }
    }
  }

  // 注册全部作废
  registeredTools = [];
  registeredProviders = [];
  registeredIpc = [];
  eventSubs.clear();

  return { ok: true, errors };
}

/** host.event：宿主事件投递给插件订阅者。单向，不回结果。 */
function doHostEvent(params) {
  const event = params.event;
  if (typeof event !== "string") return;
  if (disposed) return; // 停止中不再投递，避免插件在清理期收到事件
  dispatchLocalEvent(event, params.payload);
}

const METHODS = {
  "plugin.register": doRegister,
  "tool.execute": doToolExecute,
  "prompt.provide": doPromptProvide,
  "ipc.invoke": doIpcInvoke,
  "plugin.dispose": doDispose,
  "host.event": doHostEvent,
  // 健康探测：Python 侧确认子进程活着且协议通
  "host.ping": async () => ({ pong: true, node: process.version, pid: process.pid }),
};

// ---------------------------------------------------------------------------
// 主循环
// ---------------------------------------------------------------------------

function fail(msg, extra) {
  // 启动期致命错误：既写 stderr（Python 会收进日志），也尽力发一条协议错误，
  // 让 Python 侧不必干等超时。
  process.stderr.write(`[plugin_host] FATAL: ${msg}\n`);
  if (extra) process.stderr.write(`[plugin_host] ${safeStringify(extra)}\n`);
  send({ jsonrpc: "2.0", id: null, error: { code: "E_INTERNAL", message: msg } });
}

function main() {
  // argv: node plugin_host.cjs <pluginId>
  // 插件目录与 manifest 由 Python 通过 plugin.register 的 params 传入，
  // 不走 argv —— 避免路径里的空格与中文在命令行上被切碎。
  const tag = process.argv[2] || "plugin";
  installConsoleRedirect(tag);

  process.on("uncaughtException", (e) => {
    process.stderr.write(`[plugin_host] uncaughtException: ${e && e.stack}\n`);
  });
  process.on("unhandledRejection", (r) => {
    process.stderr.write(`[plugin_host] unhandledRejection: ${safeStringify(r && r.message || r)}\n`);
  });

  const rl = readline.createInterface({ input: process.stdin, terminal: false });

  rl.on("line", (line) => {
    const s = line.trim();
    if (!s) return; // 空行忽略（Python 侧可能发心跳换行）
    let msg;
    try {
      msg = JSON.parse(s);
    } catch (e) {
      process.stderr.write(`[plugin_host] 协议行解析失败: ${s.slice(0, 200)}\n`);
      return;
    }

    // 响应（无 method，有 id + result/error）
    if (!msg.method && msg.id !== undefined && msg.id !== null) {
      handleResponse(msg);
      return;
    }
    // 通知 / 无 id 的响应
    if (!msg.method) {
      if (msg.id === null && msg.error) handleResponse(msg);
      return;
    }

    const handler = METHODS[msg.method];
    if (!handler) {
      if (msg.id !== undefined && msg.id !== null) {
        send({
          jsonrpc: "2.0",
          id: msg.id,
          error: { code: "E_INVALID_ARGUMENT", message: `未知方法: ${msg.method}` },
        });
      }
      return;
    }

    // host.event 是单向通知，不必进 Promise 链
    Promise.resolve()
      .then(() => handler(msg.params || {}))
      .then(
        (result) => {
          if (msg.id !== undefined && msg.id !== null) {
            send({ jsonrpc: "2.0", id: msg.id, result: result === undefined ? null : result });
          }
        },
        (err) => {
          process.stderr.write(
            `[plugin_host] ${msg.method} 失败: ${err && err.stack || err}\n`
          );
          if (msg.id !== undefined && msg.id !== null) {
            const code = err && HOST_ERROR_CODES.has(err.code) ? err.code : "E_INTERNAL";
            send({
              jsonrpc: "2.0",
              id: msg.id,
              error: { code, message: (err && err.message) || String(err) },
            });
          }
        }
      );
  });

  rl.on("close", async () => {
    // Python 侧关管道 = 宿主要走了。尽力清理后退出。
    try {
      await doDispose();
    } catch (e) {
      process.stderr.write(`[plugin_host] 退出前 dispose 失败: ${e && e.message}\n`);
    }
    process.exit(0);
  });

  // 就绪信号：Python 侧等到这行才认为子进程可用（避免起进程后立刻发
  // register 却撞上 node 还没初始化完的竞态）。
  send({
    jsonrpc: "2.0",
    method: "host.ready",
    params: { node: process.version, pid: process.pid, apiVersion: SUPPORTED_API_VERSION },
  });
}

try {
  main();
} catch (e) {
  fail(e && e.message || String(e), e && e.stack);
  process.exit(1);
}

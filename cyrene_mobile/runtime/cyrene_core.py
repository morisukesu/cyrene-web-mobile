# -*- coding: utf-8 -*-
"""cyrene_core —— 昔涟手机端后端的微内核（范式对齐 DSH / cordis）。

为什么要有它
------------
`cyrene_web.py` 已经长到 565 KB / 约 12290 行，十几条子系统挤在同一份模块级
全局状态上，彼此只能靠约定分层（改一处要通读上下文才敢下手）。这个内核给它们
一个共同的挂载点：每个子系统可以**整块挂上去、整块摘下来**。

五个概念（对齐 cordis，但只用标准库）
------------------------------------
1. **Context 容器**     `Runtime` 持有服务表、插件表、事件总线，以及每个插件
                        自己的 effect 栈。
2. **ctx.<key> 服务键** 服务按稳定 key 注册与查找。消费方查 key、不 import
                        实现 —— 这样换掉提供方不会牵动消费方。
3. **inject 声明依赖**  插件声明 `requires`，依赖齐了才 `setup()`；没齐就挂着
                        等，不靠手写启动顺序。服务一旦补齐会自动重扫。
4. **effect 可逆注册**  任何注册都登记一个撤销函数，卸载时**逆序**撤销
                        （后注册的先撤，和栈的直觉一致）。
5. **事件**            `emit`（观察）/ `waterfall`（环绕中间件，可短路）/
                        `serial`（按序，首个非 None 即停）。够用即止，不追求
                        cordis 五种全齐 —— 手机端没有 HMR，多出来的模式是负担。

红线（改这个文件之前先读）
--------------------------
* **只 import 标准库**。尤其**绝对不得** import `cyrene_web` —— 内核要被宿主
  import，反过来 import 会立刻形成循环依赖。`_smoke_core.py` 里有静态自检
  盯着这一条，别绕过它。
* **新增即零影响**：没被注册进来的插件等于不存在。既有行为一个字不变。
* **不引入第三方依赖**：Termux 上装不动，也违背项目零依赖的形态。

典型用法（宿主侧）
------------------
    from cyrene_core import Runtime

    rt = Runtime(log_sink=lambda lv, m: print("[core]", lv, m))
    rt.provide("config", config_service)     # 先垫基础服务
    rt.provide("tools", tools_service)
    rt.use(search_plugin)                    # 再挂子系统插件（依赖没齐会自动等）
    rt.start()                               # 一次性把能起的都起来
    ...
    rt.stop()                                # 逆序撤销，回到没挂过的样子

插件形态（三种写法都收，见 normalize_plugin）
--------------------------------------------
    PLUGIN = {"id": "search", "requires": ["config"], "setup": setup_fn}

    class SearchPlugin:
        id = "search"
        requires = ("config",)
        def setup(self, ctx): ...          # 类是模板，内核会自动实例化一次

    def setup(ctx): ...                    # 裸函数：id 取函数名，无依赖

诊断
----
`rt.status()` 回一份快照（谁起了、谁还缺什么、错了什么），`rt.logs()` 回最近的
内核日志。宿主可以把它挂到 `/health` 或启动横幅上 —— 出问题时能一眼看见，
而不是靠猜。
"""
import sys
import threading
import time
import traceback

__all__ = [
    "Runtime",
    "PluginContext",
    "CoreError",
    "ServiceMissing",
    "PluginSetupError",
    "normalize_plugin",
    "PLUGIN_STATES",
    "CORE_VERSION",
]

CORE_VERSION = 1

# 插件状态机：pending（挂着等依赖）→ active（跑着）／failed（setup 抛错）
#              ──stop()/unuse()──> stopped
PLUGIN_STATES = ("pending", "active", "failed", "stopped")

# 内核日志的环形缓冲上限。手机上内存金贵，诊断够用就行，不落盘。
LOG_BUFFER_MAX = 400

# 事件分发模式
EVENT_MODES = ("emit", "waterfall", "serial")


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------

class CoreError(Exception):
    """内核错误的基类。"""


class ServiceMissing(CoreError):
    """要求的服务不在服务表里。"""

    def __init__(self, key, who=None):
        self.key = key
        self.who = who
        msg = "服务 %r 尚未提供" % (key,)
        if who:
            msg += "（%s 需要它）" % (who,)
        super().__init__(msg)


class PluginSetupError(CoreError):
    """插件 setup 抛错时包一层，带上插件 id 与原始异常。"""

    def __init__(self, plugin_id, cause):
        self.plugin_id = plugin_id
        self.cause = cause
        super().__init__("插件 %r 启动失败: %s: %s" % (
            plugin_id, type(cause).__name__, cause))


# ---------------------------------------------------------------------------
# 插件账本
# ---------------------------------------------------------------------------

class _PluginRecord:
    """内核里一个插件的账本。

    用 __slots__ 是为了两件事：手机上少一点内存开销；以及防止手滑往账本上
    挂临时属性 —— 那会让 status() 的快照变成一锅粥。
    """

    __slots__ = ("id", "requires", "setup", "ctx", "state", "error",
                 "missing", "order", "started_at", "stopped_at")

    def __init__(self, spec, order=0):
        self.id = spec["id"]
        self.requires = spec["requires"]
        self.setup = spec["setup"]
        self.ctx = None
        self.state = "pending"
        self.error = None
        self.missing = list(spec["requires"])
        self.order = order
        self.started_at = None
        self.stopped_at = None


def normalize_plugin(plugin):
    """把三种插件写法归一成 `{id, requires, setup}`。

    收：
      · dict        ``{"id": ..., "requires": [...], "setup": fn}``
      · 类 / 实例    有 ``id`` / ``requires`` / ``setup`` 属性
      · 裸函数       它自己就是 setup，id 取函数名

    不收的**当场报错** —— 宁可在挂载时炸得清楚，也别等运行到一半才发现
    没有 setup 可调。
    """
    # 类是模板，自动实例化一次（实例才有自己的状态）
    if isinstance(plugin, type):
        plugin = plugin()

    # 1) 裸可调用对象：它本身就是 setup
    if callable(plugin) and not isinstance(plugin, dict) and not hasattr(plugin, "setup"):
        name = getattr(plugin, "__name__", None) or "anonymous"
        return {"id": name, "requires": (), "setup": plugin}

    # 2) dict 声明
    if isinstance(plugin, dict):
        pid = plugin.get("id")
        setup = plugin.get("setup")
        requires = plugin.get("requires") or ()
    else:
        # 3) 带属性的对象
        pid = getattr(plugin, "id", None)
        setup = getattr(plugin, "setup", None)
        requires = getattr(plugin, "requires", ()) or ()
        if pid is None:
            pid = getattr(plugin, "__name__", None)

    if not pid or not isinstance(pid, str):
        raise CoreError("插件缺少可用的 id：%r" % (plugin,))
    if not callable(setup):
        raise CoreError("插件 %r 没有可调用的 setup" % (pid,))

    # 单个字符串写成 requires="config" 是常见的手滑，顺手认掉
    if isinstance(requires, str):
        requires = (requires,)

    return {"id": pid, "requires": tuple(str(k) for k in requires), "setup": setup}


# ---------------------------------------------------------------------------
# 插件上下文
# ---------------------------------------------------------------------------

class PluginContext:
    """插件自己的上下文视图。

    它是 `Runtime` 的一层薄代理：**服务查找**转发给 Runtime（服务是全局面共享
    的），而 **effect 记在自己账上**（卸载时只撤自己登记的那些，不会误伤别人）。

    插件拿到 ctx 之后应该只通过它办事，别去抓 Runtime 的内部字典 —— 那会让
    "能整块摘下来"的保证失效。
    """

    __slots__ = ("_rt", "_pid", "_effects", "_active")

    def __init__(self, rt, pid):
        self._rt = rt
        self._pid = pid
        self._effects = []       # [(desc, undo)]，按登记顺序；撤销时反向走
        self._active = True

    # ---------------- 身份 ----------------

    @property
    def plugin_id(self):
        return self._pid

    @property
    def runtime(self):
        return self._rt

    def __repr__(self):
        return "<PluginContext %s effects=%d>" % (self._pid, len(self._effects))

    # ---------------- 服务 ----------------

    def get(self, key, default=None):
        return self._rt.get(key, default)

    def has(self, key):
        return self._rt.has(key)

    def require(self, key):
        """拿服务；没有就抛。适合"少它我就没法活"的硬依赖。"""
        return self._rt.require(key, who=self._pid)

    def provide(self, key, service, replace=False):
        """把一个服务放进内核，卸载时自动摘掉。

        已被占用又没给 replace 时**直接报错** —— 静默覆盖会让两个插件的
        行为都变得不可预测。
        """
        undo = self._rt._do_provide(key, service, replace=replace, owner=self._pid)
        self.effect(undo, desc="provide:%s" % key)
        return service

    # ---------------- 事件 ----------------

    def on(self, event, fn, prepend=False, mode="emit"):
        """注册一个监听器，卸载时自动摘掉。返回撤销函数。"""
        undo = self._rt._add_listener(event, fn, prepend=prepend, mode=mode,
                                      owner=self._pid)
        self.effect(undo, desc="on:%s" % event)
        return undo

    def emit(self, event, *args):
        return self._rt.emit(event, *args)

    def waterfall(self, event, *args):
        return self._rt.waterfall(event, *args)

    def serial(self, event, *args):
        return self._rt.serial(event, *args)

    # ---------------- 可逆注册 ----------------

    def effect(self, undo, desc=""):
        """登记一个"撤销函数"。卸载时按**逆序**调用。

        如果插件已经卸载了还来登记，就**立刻撤销掉** —— 不能让它留在空气里，
        否则就是一份谁也管不着的资源。
        """
        if not callable(undo):
            raise CoreError("effect 需要一个可调用的撤销函数，收到 %r" % (undo,))

        if not self._active:
            try:
                undo()
            except Exception as e:
                self._rt._log("error", "插件 %s 卸载后仍登记了 effect（已就地撤销，但撤销失败：%s）"
                              % (self._pid, e))
            return undo

        self._effects.append((desc or "effect", undo))
        return undo

    def bind(self, container, key, value):
        """往一个 **dict 注册表**里塞值，并登记"卸载时拿回来"。

        `TOOLS`、`TOOL_HANDLERS`、路由表这类注册表直接用
        `ctx.bind(TOOLS, "web_search", spec)` 就能挂上；摘下来时原样还原
        （原本没有的键会被删掉，不留下 None 这种脏值）。

        list 请改用 `ctx.append`。两者参数个数本来就不同（dict 要 key+value，
        list 只要 value），硬合成一个方法会让签名变得含糊 —— 少传一个参数时
        报的是"缺参数"这种难查的错，不如让方法名说清楚。
        """
        if not isinstance(container, dict):
            raise CoreError("bind 只用于 dict 注册表，收到 %s；list 请用 ctx.append"
                            % (type(container).__name__,))

        had = key in container
        old = container.get(key)

        def undo():
            if had:
                container[key] = old
            else:
                container.pop(key, None)

        self.effect(undo, desc="bind:%s" % (key,))
        container[key] = value
        return value

    def append(self, container, value):
        """往一个 **list** 里追加元素，并登记"卸载时移除"。"""
        if not isinstance(container, list):
            raise CoreError("append 只用于 list，收到 %s；dict 请用 ctx.bind"
                            % (type(container).__name__,))

        def undo():
            try:
                container.remove(value)
            except ValueError:
                # 已经被别人摘掉了，不算错
                pass

        self.effect(undo, desc="append")
        container.append(value)
        return value

    # ---------------- 日志 ----------------

    def log(self, msg, level="info"):
        self._rt._log(level, "[%s] %s" % (self._pid, msg))

    # ---------------- 内部 ----------------

    def _revert(self):
        """逆序撤销自己登记的全部副作用。返回撤销失败的原因列表。"""
        self._active = False
        errs = []
        while self._effects:
            desc, undo = self._effects.pop()
            try:
                undo()
            except Exception as e:
                errs.append("%s: %s: %s" % (desc, type(e).__name__, e))
        return errs


# ---------------------------------------------------------------------------
# 内核
# ---------------------------------------------------------------------------

class Runtime:
    """微内核：服务表 + 插件表 + 事件总线。

    线程安全：内部用一把可重入锁护住服务表与事件表。**分发事件时不持锁**
    （先取快照再调用），否则监听器里回手调一次内核就会自锁。
    """

    def __init__(self, log_sink=None, name="cyrene"):
        self.name = name
        self._services = {}          # key -> service
        self._service_owner = {}     # key -> 提供方 id（诊断用）
        self._listeners = {}         # event -> [(fn, mode, owner)]
        self._plugins = []           # [_PluginRecord]，保持挂载顺序
        self._by_id = {}
        self._logs = []
        self._log_sink = log_sink
        self._lock = threading.RLock()
        self._started = False
        self._rescanning = False     # 防 rescan 递归

    def __repr__(self):
        return "<Runtime %s plugins=%d services=%d>" % (
            self.name, len(self._plugins), len(self._services))

    # ================= 服务 =================

    def _do_provide(self, key, service, replace=False, owner="<host>"):
        """真正写服务表并返回 undo。Runtime.provide 与 ctx.provide 都走这里。"""
        if not isinstance(key, str) or not key:
            raise CoreError("服务 key 必须是非空字符串，收到 %r" % (key,))

        with self._lock:
            if key in self._services and not replace:
                raise CoreError("服务 %r 已被 %s 提供（要用 replace=True 覆盖）"
                                % (key, self._service_owner.get(key) or "?"))
            had = key in self._services
            old = self._services.get(key)
            old_owner = self._service_owner.get(key)
            self._services[key] = service
            self._service_owner[key] = owner

        def undo():
            with self._lock:
                # 只在"当前值还是我塞的那个"时才摘 —— 避免把后来者的服务误删
                if self._services.get(key) is service:
                    if had:
                        self._services[key] = old
                        self._service_owner[key] = old_owner
                    else:
                        self._services.pop(key, None)
                        self._service_owner.pop(key, None)
            # 服务表变了，可能刚好让哪个挂着的插件依赖齐了
            self.rescan()

        self.rescan()
        return undo

    def provide(self, key, service, replace=False, owner="<host>"):
        """宿主直接提供一个基础服务。返回撤销函数。

        没 start() 之前调用也安全 —— 只是登记进服务表，等 start() 再统一放行
        挂着等依赖的插件。
        """
        return self._do_provide(key, service, replace=replace, owner=owner)

    def get(self, key, default=None):
        with self._lock:
            return self._services.get(key, default)

    def has(self, key):
        with self._lock:
            return key in self._services

    def require(self, key, who=None):
        with self._lock:
            if key in self._services:
                return self._services[key]
        raise ServiceMissing(key, who=who)

    def services(self):
        with self._lock:
            return sorted(self._services.keys())

    # ================= 插件 =================

    def use(self, plugin):
        """挂一个插件。**挂上不等于启动** —— 依赖齐了才会 setup。

        重复挂同一个 id 会报错：多半是调用方写错了，静默忽略反而会掩盖问题。
        """
        spec = normalize_plugin(plugin)
        with self._lock:
            if spec["id"] in self._by_id:
                raise CoreError("插件 %r 已经挂过了" % (spec["id"],))
            rec = _PluginRecord(spec, order=len(self._plugins))
            self._plugins.append(rec)
            self._by_id[rec.id] = rec

        self._log("info", "挂载插件 %s（依赖：%s）"
                  % (rec.id, ", ".join(rec.requires) or "无"))

        # 已经启动过就顺手扫一遍，让热挂的插件该起就起
        if self._started:
            self.rescan()
        return rec.id

    def unuse(self, plugin_id):
        """单独摘掉一个插件（热卸载）。返回撤销失败的原因列表。"""
        with self._lock:
            rec = self._by_id.pop(plugin_id, None)
            if rec is None:
                raise CoreError("没有这个插件：%r" % (plugin_id,))
            try:
                self._plugins.remove(rec)
            except ValueError:
                pass
        return self._unsetup(rec)

    def start(self, strict=False):
        """把所有依赖已满足的插件启动起来，返回本次启动的插件 id 列表。

        strict=False（默认）时单个插件 setup 抛错会被**隔离**：记进账本、
        标记 failed，其余插件照常启动。手机上"一个子系统坏了拖垮整个服务"
        是最不该发生的事。
        """
        with self._lock:
            self._started = True
        started = self.rescan(strict=strict)
        return started

    def rescan(self, strict=False):
        """扫一遍 pending 的插件，依赖齐的就 setup。

        设计成可反复调用且幂等的：`provide()` 之后会自动触发，所以插件不用
        关心"我依赖的东西什么时候到"。启动一个插件可能让另一个的依赖变齐，
        因此这里循环到没有新进展为止。
        """
        if self._rescanning:
            # 递归进来（插件在 setup 里又 provide 了）：直接返回，
            # 外层那一次会继续把剩下的扫完，不需要嵌套
            return []

        self._rescanning = True
        started = []
        try:
            while True:
                with self._lock:
                    pending = [r for r in self._plugins if r.state == "pending"]
                if not pending:
                    break

                progressed = False
                for rec in pending:
                    # 再确认一次状态：上一轮循环里它可能已经被启动了
                    if rec.state != "pending":
                        continue

                    missing = [k for k in rec.requires if not self.has(k)]
                    rec.missing = missing
                    if missing:
                        continue

                    # 只有真启动成功才记进"已启动"清单 —— 失败的已被隔离成
                    # failed，不该混进去让调用方以为它起来了
                    if self._setup(rec, strict=strict):
                        started.append(rec.id)
                    # 但不论成败都算"有进展"：失败的插件状态已不是 pending，
                    # 下一轮不会再看它，靠这个避免死循环
                    progressed = True

                if not progressed:
                    break          # 剩下的都在等依赖，等 provide 再唤醒
        finally:
            self._rescanning = False

        return started

    def stop(self):
        """逆序卸载所有插件，撤销各自登记的副作用。返回撤销失败的原因列表。"""
        with self._lock:
            self._started = False
            recs = list(self._plugins)

        errs = []
        for rec in reversed(recs):
            errs.extend(self._unsetup(rec))

        self._log("info", "内核已停止（%d 个插件）" % (len(recs),))
        return errs

    def _setup(self, rec, strict=False):
        """启动一个插件。返回是否成功。"""
        ctx = PluginContext(self, rec.id)
        rec.ctx = ctx
        try:
            rec.setup(ctx)
        except Exception as e:
            rec.state = "failed"
            rec.error = "%s: %s" % (type(e).__name__, e)
            # 起一半失败也要把已登记的副作用撤干净，
            # 否则会留下一半挂着的注册 —— 那比没启动更危险
            ctx._revert()
            rec.ctx = None
            self._log("error", "插件 %s 启动失败：%s\n%s"
                      % (rec.id, e, traceback.format_exc()))
            if strict:
                raise PluginSetupError(rec.id, e)
            return False

        rec.state = "active"
        rec.error = None
        rec.missing = []
        rec.started_at = time.time()
        self._log("info", "插件 %s 已启动" % (rec.id,))
        return True

    def _unsetup(self, rec):
        """卸载一个插件，返回撤销失败的原因列表。"""
        errs = []
        ctx = rec.ctx
        if ctx is not None:
            errs = ctx._revert()
        rec.ctx = None
        if rec.state != "stopped":
            rec.state = "stopped"
            rec.stopped_at = time.time()
        if errs:
            self._log("error", "插件 %s 卸载时有 %d 处撤销失败：%s"
                      % (rec.id, len(errs), "; ".join(errs)))
        return errs

    def plugin_ids(self):
        with self._lock:
            return [r.id for r in self._plugins]

    # ================= 事件 =================

    def _add_listener(self, event, fn, prepend=False, mode="emit", owner=None):
        """加一个监听器，返回摘除函数。"""
        if mode not in EVENT_MODES:
            raise CoreError("不认识的事件模式 %r（可选 %s）"
                            % (mode, "/".join(EVENT_MODES)))
        if not callable(fn):
            raise CoreError("监听器必须是可调用的，收到 %r" % (fn,))

        entry = (fn, mode, owner)
        with self._lock:
            lst = self._listeners.setdefault(event, [])
            if prepend:
                lst.insert(0, entry)
            else:
                lst.append(entry)

        def undo():
            with self._lock:
                lst = self._listeners.get(event)
                if not lst:
                    return
                for i, item in enumerate(lst):
                    if item is entry:
                        lst.pop(i)
                        break
                if not lst:
                    self._listeners.pop(event, None)

        return undo

    def on(self, event, fn, prepend=False, mode="emit", owner="<host>"):
        """宿主注册监听器，返回撤销函数（宿主自己负责在收尾时调用）。"""
        return self._add_listener(event, fn, prepend=prepend, mode=mode, owner=owner)

    def _snapshot(self, event, mode):
        """取该模式下监听器的**快照**。

        为什么要快照：分发过程中监听器可能增删（甚至卸载自己），直接在原列表上
        迭代会踩到"边遍历边改"。同时也是不持锁分发的关键。
        """
        with self._lock:
            return [item for item in self._listeners.get(event, ()) if item[1] == mode]

    def emit(self, event, *args):
        """观察式分发：按注册顺序全部调用，忽略返回值。

        单个监听器抛错**只记账不外抛** —— 观察者不该拖垮发布方。
        """
        for fn, _mode, owner in self._snapshot(event, "emit"):
            try:
                fn(*args)
            except Exception as e:
                self._log("error", "事件 %s 的监听器（%s）出错：%s: %s"
                          % (event, owner or "?", type(e).__name__, e))
        return None

    def waterfall(self, event, *args):
        """环绕中间件式分发。

        监听器签名 `fn(*args, nxt)`。调用 `nxt()` 会执行下游并把它返回的结果
        交回这一层，这一层可以包装后再往外传；**不调 nxt() 直接返回 = 短路**，
        下游不再执行（单决策事件里这正是想要的效果）。

        重复调用 nxt() 不会让下游跑第二遍，返回上一次的结果。
        没有监听器时返回 None。
        """
        chain = self._snapshot(event, "waterfall")
        if not chain:
            return None

        def dispatch(i, *a):
            if i >= len(chain):
                return None
            fn = chain[i][0]
            state = {"called": False, "inner": None}

            def nxt(*extra):
                if state["called"]:
                    return state["inner"]
                state["called"] = True
                state["inner"] = dispatch(i + 1, *(extra if extra else a))
                return state["inner"]

            try:
                out = fn(*a, nxt)
            except Exception as e:
                self._log("error", "事件 %s 的 waterfall 监听器（%s）出错：%s: %s"
                          % (event, chain[i][2] or "?", type(e).__name__, e))
                out = None
                state["called"] = True          # 出错就当它没短路，继续往下

            if not state["called"]:
                return out                      # 短路
            if out is not None:
                return out                      # 包装后返回
            return state["inner"]

        return dispatch(0, *args)

    def serial(self, event, *args):
        """按序分发：首个返回非 None 的监听器即停，其余不再执行。"""
        for fn, _mode, owner in self._snapshot(event, "serial"):
            try:
                r = fn(*args)
            except Exception as e:
                self._log("error", "事件 %s 的 serial 监听器（%s）出错：%s: %s"
                          % (event, owner or "?", type(e).__name__, e))
                continue
            if r is not None:
                return r
        return None

    def events(self):
        with self._lock:
            return {k: len(v) for k, v in self._listeners.items() if v}

    # ================= 诊断 =================

    def status(self):
        """一份快照：谁起了、谁还缺什么、错了什么。

        建议宿主把它挂到 `/health` 或启动横幅 —— 出问题时能一眼看见，
        而不是靠猜。
        """
        with self._lock:
            plugins = [{
                "id": r.id,
                "state": r.state,
                "requires": list(r.requires),
                "missing": list(r.missing),
                "error": r.error,
            } for r in self._plugins]
            services = {k: (self._service_owner.get(k) or "?") for k in self._services}
            events = {k: len(v) for k, v in self._listeners.items() if v}

        return {
            "coreVersion": CORE_VERSION,
            "name": self.name,
            "started": self._started,
            "plugins": plugins,
            "services": services,
            "events": events,
        }

    def summary(self):
        """一行话的诊断摘要，适合塞进启动日志。"""
        st = self.status()
        parts = []
        for p in st["plugins"]:
            if p["state"] == "active":
                parts.append("%s✓" % p["id"])
            elif p["state"] == "failed":
                parts.append("%s✗" % p["id"])
            elif p["state"] == "pending":
                parts.append("%s…(缺%s)" % (p["id"], ",".join(p["missing"]) or "?"))
            else:
                parts.append("%s·" % p["id"])
        return "内核：%d 服务 / 插件 %s" % (
            len(st["services"]), " ".join(parts) if parts else "（无）")

    def logs(self, limit=50):
        with self._lock:
            return list(self._logs[-limit:])

    def _log(self, level, msg):
        item = {"t": time.time(), "level": level, "msg": msg}
        with self._lock:
            self._logs.append(item)
            if len(self._logs) > LOG_BUFFER_MAX:
                del self._logs[:len(self._logs) - LOG_BUFFER_MAX]

        sink = self._log_sink
        if sink is not None:
            try:
                sink(level, msg)
            except Exception:
                # 日志出口自己出问题不能反过来影响内核
                pass

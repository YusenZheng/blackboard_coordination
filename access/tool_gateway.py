# STATUS: STAGED(A类)—— MVP:五类 Tool 注册 + dispatch(实现 G + mock Y,S/C/V 接口位)
"""access.tool_gateway —— Tool 网关。实现 contracts.interfaces.ToolGatewayPort。

【A6】Tool 五类,不只 G 系列下行:
    G 设备端原子工具(定点导航/区域扫描/热成像…)—— 走适配器下发设备
    S 态势分析工具(躲藏趋势/接力时机/最优设备推荐…)—— 云端确定性工具,不是设备指令
    C 异构算子协同工具 —— 任务/资源协同动作
    V 社交动词(bid/claim/yield/handoff/ask_help)—— 写黑板,不下发设备
    Y 云端工具(任务发布/线索融合/技能注册…)
【C6 注】C 与 V 语义有重叠(C03/04/05 vs V02/03/04),避免重复建模——V 走黑板动词、
    C 走工具调用,同一意图两种载体,dispatch 时归一。

MVP:实现 G(经适配器)+ 一个 mock Y;S/C/V 建 dispatch 分支 + 接口位,标 TODO。
"""
from __future__ import annotations

from enum import Enum

from ..contracts.task import ActionIntent, ActionReceipt


class ToolClass(str, Enum):
    G = "G"   # 设备端原子工具
    S = "S"   # 态势分析(云端)
    C = "C"   # 异构算子协同
    V = "V"   # 社交动词
    Y = "Y"   # 云端工具


class ToolGateway:
    def __init__(
        self,
        adapters: dict = None,
        tool_registry=None,
        estop_bus=None,
        *,
        device_registry=None,
        trace_listener=None,
    ):
        self._adapters = adapters or {}   # device_id -> AdapterPort
        self._estop = estop_bus           # B1:急停总线,下发前查
        self._device_registry = device_registry
        self._trace_listener = trace_listener
        self._runtime = None
        # Tool 注册表:call_tool 从这里查具体 Tool(而非靠前缀猜)
        if tool_registry is None:
            from .tools.base import load_builtin_tools
            tool_registry = load_builtin_tools()
        self._registry = tool_registry
        self.catalog = tool_registry

    def dispatch(self, intent: ActionIntent) -> ActionReceipt:
        """把【已过安全校验】的动作意图(G 系列)翻译成设备指令、收回执。

        TODO(接缝·归一层缺失):当前 dispatch 走 intent.verb + device_id 直接找 adapter,
            call_tool 走 tool_id 找 ToolRegistry —— 两条入口不相交。
            协同层产出 ActionIntent(verb=ActionVerb 枚举,如 move_to),但 ToolSpec 用 tool_id
            索引(如 G01),二者之间【无 verb→tool_id 映射】,params 键也不归一
            (意图侧 target vs ToolSpec 侧 destination)。
            需补:verb→tool_id 归一 + params 键归一,让 dispatch 能落到 ToolSpec。
            前置:命名词表要先锁一套(Card 能力 / TaskRequirement / ActionVerb / tool_id 现为三套),
            词表由总监拍定后再实现此映射(改契约走审批)。
        """
        # B1 设备侧硬门控:急停生效期间,任何动作下发前被拦(急停后不再照发下一个 intent)
        if self._estop is not None and self._estop.is_stopped(intent.device_id):
            return ActionReceipt(intent_id=intent.intent_id, device_id=intent.device_id,
                                 success=False, failure_reason="设备处于急停状态,拒绝执行",
                                 recovery_class="need_human")
        adapter = self._adapters.get(intent.device_id)
        if adapter is None:
            return ActionReceipt(intent_id=intent.intent_id, device_id=intent.device_id,
                                 success=False, failure_reason="no adapter for device",
                                 recovery_class="need_reassign")
        receipt = adapter.execute(intent)
        tool_id = {"move_to": "G01"}.get(getattr(intent.verb, "value", intent.verb))
        if tool_id:
            receipt.extra.setdefault("tool_id", tool_id)
        if intent.extra.get("skill_references"):
            receipt.extra["skill_references"] = intent.extra["skill_references"]
        if self._trace_listener is not None:
            try:
                self._trace_listener({
                    "trace_id": intent.extra.get("trace_id", intent.intent_id),
                    "call_id": intent.intent_id,
                    "task_id": intent.task_id,
                    "device_id": intent.device_id,
                    "tool_id": tool_id,
                    "success": receipt.success,
                    "binding": {
                        "device_id": intent.device_id,
                        "tool_id": tool_id,
                        "status": "available",
                    },
                    "skill_references": intent.extra.get("skill_references", []),
                })
            except Exception:
                pass
        return receipt

    def invoke(self, request):
        """统一 Tool 调用入口；旧 dispatch/call_tool API 保持兼容。"""
        if self._runtime is None:
            from .tool_runtime import ToolRuntime
            self._runtime = ToolRuntime(
                catalog=self._registry,
                device_registry=self._device_registry,
                adapters=self._adapters,
                trace_listener=self._trace_listener,
            )
        return self._runtime.invoke(request)

    def call_tool(self, tool_id: str, params: dict) -> dict:
        """调用五类 Tool 之一:先查注册表,注册了就调它的 run;没注册按类给占位。"""
        spec = self._registry.get(tool_id)
        if spec is not None and spec.run is not None:
            return spec.run(params)
        # 未注册的 Tool(尚未补):按类给占位,别静默假成功
        cls = self._classify(tool_id)
        if cls in (ToolClass.S, ToolClass.C, ToolClass.V):
            return {"tool": tool_id, "class": cls.value, "result": "NOT-STAGED(照 IO 设计补)"}
        return {"tool": tool_id, "class": cls.value, "result": "NOT-REGISTERED",
                "note": "在 access/tools/ 下照 g01_navigate 样例补并 register"}

    @staticmethod
    def _classify(tool_id: str) -> ToolClass:
        prefix = tool_id[:1].upper()
        try:
            return ToolClass(prefix)
        except ValueError:
            return ToolClass.G

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
from uuid import uuid4

from ..contracts.task import ActionIntent, ActionReceipt
from ..contracts.tooling import ToolCallRequest
from .tool_runtime import ToolRuntime


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
        blackboard=None,
        trace_listener=None,
    ):
        self._adapters = adapters or {}   # device_id -> AdapterPort
        self._estop = estop_bus           # B1:急停总线,下发前查
        # Tool 注册表:call_tool 从这里查具体 Tool(而非靠前缀猜)
        if tool_registry is None:
            from .tools.base import load_builtin_tools
            tool_registry = load_builtin_tools()
        self._registry = tool_registry
        self.catalog = tool_registry
        self._device_registry = device_registry
        self.dispatched_intent_ids: list[str] = []
        self._runtime = ToolRuntime(
            catalog=tool_registry,
            device_registry=device_registry,
            adapters=self._adapters,
            blackboard=blackboard,
            trace_listener=trace_listener,
        )

    def is_available(self, device_id: str, verb: str) -> bool:
        """按 Agent Card、动作动词和适配器绑定检查设备 Tool 是否可用。"""
        return self._runtime.is_available(device_id, verb)

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
        self.dispatched_intent_ids.append(intent.intent_id)
        # B1 设备侧硬门控:急停生效期间,任何动作下发前被拦(急停后不再照发下一个 intent)
        if self._estop is not None and self._estop.is_stopped(intent.device_id):
            return ActionReceipt(intent_id=intent.intent_id, device_id=intent.device_id,
                                 success=False, failure_reason="设备处于急停状态,拒绝执行",
                                 recovery_class="need_human")
        spec = self.catalog.for_action_verb(intent.verb)
        if spec is not None and self._device_registry is not None:
            result = self.invoke(
                ToolCallRequest(
                    schema_version="2.0",
                    call_id=intent.intent_id,
                    tool_id=spec.tool_id,
                    arguments=dict(intent.params),
                    task_id=intent.task_id,
                    device_id=intent.device_id,
                    trace_id=intent.extra.get("trace_id"),
                    idempotency_key=intent.extra.get(
                        "idempotency_key", intent.intent_id
                    ),
                    action_intent=intent,
                    context=dict(intent.extra),
                )
            )
            if result.receipt is not None:
                return result.receipt
            return ActionReceipt(
                intent_id=intent.intent_id,
                device_id=intent.device_id,
                success=False,
                failure_reason=result.error_message,
                recovery_class=(
                    "retryable" if result.retryable else "need_reassign"
                ),
                extra={"tool_id": result.tool_id, "error_code": result.error_code},
            )

        adapter = self._adapters.get(intent.device_id)
        if adapter is None:
            return ActionReceipt(intent_id=intent.intent_id, device_id=intent.device_id,
                                 success=False, failure_reason="no adapter for device",
                                 recovery_class="need_reassign")
        return adapter.execute(intent)

    def invoke(self, request: ToolCallRequest):
        """统一 Tool 调用入口，返回结构化 ToolCallResult。"""
        return self._runtime.invoke(request)

    def call_tool(self, tool_id: str, params: dict) -> dict:
        """调用五类 Tool 之一:先查注册表,注册了就调它的 run;没注册按类给占位。"""
        spec = self._registry.get(tool_id)
        if spec is not None:
            result = self.invoke(
                ToolCallRequest(
                    schema_version="2.0",
                    call_id=f"legacy-{uuid4().hex}",
                    tool_id=tool_id,
                    arguments=dict(params),
                )
            )
            if result.success:
                return result.result
            return {
                "tool": result.tool_id,
                "result": "FAILED",
                "error_code": result.error_code,
                "error_message": result.error_message,
            }
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

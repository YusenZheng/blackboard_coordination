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
from ..contracts.tooling import ToolCallRequest


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
        self._device_registry = device_registry
        self._blackboard = blackboard
        self._trace_listener = trace_listener
        self._runtime = None
        self.dispatched_intent_ids: list[str] = []
        # Tool 注册表:call_tool 从这里查具体 Tool(而非靠前缀猜)
        if tool_registry is None:
            from .tools.base import load_builtin_tools
            tool_registry = load_builtin_tools()
        self._registry = tool_registry
        self.catalog = tool_registry

    def is_available(self, device_id: str, verb: str) -> bool:
        """Resolve a coordination action verb through the WHX Tool catalog."""
        if self._estop is not None and self._estop.is_stopped(device_id):
            return False
        return self._tool_runtime().is_available(device_id, verb)

    def dispatch(self, intent: ActionIntent) -> ActionReceipt:
        """把已过安全校验的 ActionIntent 归一为 WHX ToolRuntime 调用。"""
        # B1 设备侧硬门控:急停生效期间,任何动作下发前被拦(急停后不再照发下一个 intent)
        if self._estop is not None and self._estop.is_stopped(intent.device_id):
            return ActionReceipt(intent_id=intent.intent_id, device_id=intent.device_id,
                                 success=False, failure_reason="设备处于急停状态,拒绝执行",
                                 recovery_class="need_human",
                                 extra={"outcome_certainty": "confirmed"})

        spec = self._registry.for_action_verb(intent.verb)
        if spec is None:
            return ActionReceipt(
                intent_id=intent.intent_id,
                device_id=intent.device_id,
                success=False,
                failure_reason=f"no Tool registered for action verb {intent.verb.value}",
                recovery_class="unrecoverable",
                extra={"outcome_certainty": "confirmed"},
            )

        arguments = dict(intent.params)
        # G01 历史 IO 使用 destination；v2 ActionIntent 使用 target。
        if "target" in arguments and "destination" not in arguments:
            arguments["destination"] = arguments["target"]
        arguments.setdefault("device", intent.device_id)
        result = self._tool_runtime().invoke(
            ToolCallRequest(
                schema_version="2.0",
                call_id=intent.intent_id,
                tool_id=spec.tool_id,
                arguments=arguments,
                task_id=intent.task_id,
                agent_id=intent.device_id,
                device_id=intent.device_id,
                trace_id=str(intent.extra.get("trace_id", intent.intent_id)),
                attempt=int(intent.extra.get("attempt", 1)),
                idempotency_key=intent.intent_id,
                action_intent=intent,
                context={
                    "skill_references": list(
                        intent.extra.get("skill_references", [])
                    )
                },
            )
        )
        if result.receipt is not None:
            receipt = result.receipt
            receipt.extra.setdefault("tool_id", result.tool_id)
            receipt.extra.setdefault("outcome_certainty", result.outcome_certainty)
            if intent.extra.get("skill_references"):
                receipt.extra["skill_references"] = intent.extra["skill_references"]
            if intent.intent_id not in self.dispatched_intent_ids:
                self.dispatched_intent_ids.append(intent.intent_id)
            return receipt

        binding = result.binding
        binding_snapshot = (
            {
                "device_id": binding.device_id,
                "tool_id": binding.tool_id,
                "executor_id": binding.executor_id,
                "adapter_id": binding.adapter_id,
                "status": binding.status,
                "reason_codes": list(binding.reason_codes),
            }
            if binding is not None
            else None
        )
        recovery_class = (
            "need_human"
            if result.outcome_certainty == "unknown"
            else "need_reassign"
            if result.error_code in {"UNAVAILABLE", "EXECUTION_FAILED"}
            else "unrecoverable"
        )
        return ActionReceipt(
            intent_id=intent.intent_id,
            device_id=intent.device_id,
            success=False,
            failure_reason=result.error_message or result.error_code,
            recovery_class=recovery_class,
            duration_s=max(0.0, result.duration_ms / 1000.0),
            extra={
                "tool_id": result.tool_id,
                "tool_error_code": result.error_code,
                "tool_binding": binding_snapshot,
                "outcome_certainty": result.outcome_certainty,
                "skill_references": list(
                    intent.extra.get("skill_references", [])
                ),
            },
        )

    def invoke(self, request):
        """统一 Tool 调用入口；旧 dispatch/call_tool API 保持兼容。"""
        return self._tool_runtime().invoke(request)

    def _tool_runtime(self):
        if self._runtime is None:
            from .tool_runtime import ToolRuntime
            self._runtime = ToolRuntime(
                catalog=self._registry,
                device_registry=self._device_registry,
                adapters=self._adapters,
                blackboard=self._blackboard,
                trace_listener=self._trace_listener,
            )
        return self._runtime

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

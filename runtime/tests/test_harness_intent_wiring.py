from __future__ import annotations

import unittest

from swarm_brain.runtime.harness import Harness


class RecordingIntentInterpreter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def interpret(self, raw_input: str, sequence: int) -> dict:
        self.calls.append((raw_input, sequence))
        return {
            "task_id": f"model_task_{sequence:03d}",
            "task_type": "search_target",
            "goal": f"模型解析:{raw_input}",
            "success_condition": "目标被确认",
            "priority": "high",
            "required_capability_ids": ["search"],
            "area": {"label": "公园", "area": "公园"},
        }


class HarnessIntentWiringTest(unittest.TestCase):
    def test_harness_injects_interpreter_into_task_gen(self) -> None:
        interpreter = RecordingIntentInterpreter()
        harness = Harness(intent_interpreter=interpreter)

        task = harness.task_gen.generate("帮我找狗")

        self.assertEqual([("帮我找狗", 1)], interpreter.calls)
        self.assertEqual("model_task_001", task.task_id)
        self.assertEqual("模型解析:帮我找狗", task.goal)
        self.assertIs(interpreter, harness.intent_interpreter)


if __name__ == "__main__":
    unittest.main()

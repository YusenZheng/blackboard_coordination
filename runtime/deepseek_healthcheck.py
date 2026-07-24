"""One-request DeepSeek connectivity health check; never prints the API key."""
from __future__ import annotations

import json

from .deepseek import DeepSeekClient, DeepSeekConfig


def run() -> dict:
    config = DeepSeekConfig.from_env()
    client = DeepSeekClient(config)
    result = client.chat_json(
        [
            {
                "role": "system",
                "content": (
                    '只输出 JSON object，格式为 {"status":"ok"}，不输出其他内容。'
                ),
            },
            {"role": "user", "content": "connectivity check"},
        ],
        thinking=False,
        operation="healthcheck",
    )
    public_result = {
        "status": result.get("status"),
        "model": config.model,
        "base_url": config.base_url,
    }
    print(json.dumps(public_result, ensure_ascii=False))
    return public_result


if __name__ == "__main__":
    run()

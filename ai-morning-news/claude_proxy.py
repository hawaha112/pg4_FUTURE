#!/usr/bin/env python3
"""
Claude Max API Proxy - 极简版
将 Claude Code CLI 的 Max 订阅额度暴露为 OpenAI 兼容 API。

用法：python3 claude_proxy.py [端口号，默认3456]

原理：接收 OpenAI 格式请求 → 调用 claude --print → 返回 OpenAI 格式响应
支持 response_format.json_schema 透传到 CLI 的 --json-schema 参数，保证 100% 有效 JSON 输出。
"""

from __future__ import annotations  # PEP 604 `dict | None` 兼容 Python 3.9
import json
import os
import re
import subprocess
import sys
import time
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 3456


class ProxyHandler(BaseHTTPRequestHandler):
    """处理 OpenAI 兼容的 API 请求。"""

    def log_message(self, format, *args):
        """自定义日志格式。"""
        print(f"  [{self.log_date_time_string()}] {format % args}")

    def _send_json(self, code: int, data: dict):
        """发送 JSON 响应。"""
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        """CORS 预检。"""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            self._send_json(200, {
                "status": "ok",
                "provider": "claude-code-cli",
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            })
        elif path in ("/v1/models", "/models"):
            self._send_json(200, {
                "object": "list",
                "data": [
                    {"id": "claude-sonnet-4", "object": "model", "owned_by": "anthropic"},
                    {"id": "claude-opus-4", "object": "model", "owned_by": "anthropic"},
                    {"id": "claude-haiku-4", "object": "model", "owned_by": "anthropic"},
                ],
            })
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ("/v1/chat/completions", "/chat/completions"):
            self._send_json(404, {"error": "not found"})
            return

        # 读取请求体
        content_length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(content_length)
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            self._send_json(400, {"error": {"message": "Invalid JSON", "type": "invalid_request_error"}})
            return

        messages = body.get("messages", [])
        if not messages:
            self._send_json(400, {"error": {"message": "messages is required", "type": "invalid_request_error"}})
            return

        # 解析模型 → CLI model 参数
        model_raw = body.get("model", "claude-sonnet-4")
        if "opus" in model_raw:
            cli_model = "opus"
        elif "haiku" in model_raw:
            cli_model = "haiku"
        else:
            cli_model = "sonnet"

        # 把 messages 拼成 prompt
        prompt = self._build_prompt(messages)

        # 提取 JSON Schema → 把完整 schema 注入 prompt 末尾。
        # 历史问题：仅注入"输出合法 JSON"的告诫文字、丢弃 schema 内容，
        # 导致 schema.required 完全无效（event_signature 等关键字段被 LLM 省略，
        # 跨语聚类失效 multi_source_count=0）。现在把 schema 真正给到模型。
        # 仍然不用 --json-schema CLI 参数（该参数会让 claude CLI 挂起）。
        schema = self._extract_json_schema(body)

        if schema:
            schema_str = json.dumps(schema, ensure_ascii=False, indent=2)
            required = schema.get("required") or []
            required_hint = (
                f"\n\n以下字段是 schema.required，缺失或留空 = 整次输出作废："
                f"\n  {', '.join(required)}"
            ) if required else ""
            prompt += (
                "\n\n[CRITICAL] 你必须输出严格遵守以下 JSON Schema 的合法 JSON。"
                "第一个字符必须是 {，最后一个字符必须是 }。"
                "禁止输出 ```json 代码块、解释文字或任何非 JSON 内容。"
                "所有字段必须认真填写，不允许留空字符串。"
                f"{required_hint}"
                f"\n\nJSON Schema:\n```json\n{schema_str}\n```"
            )

        # 构建命令行参数列表（不用 shell=True，避免管道/stdin 挂起）
        cmd = ["claude", "-p", prompt, "--model", cli_model]

        print(f"  📤 调用 claude -p --model {cli_model}（prompt {len(prompt)} 字符{', +schema-in-prompt' if schema else ''}）")

        # 调用 Claude CLI（直接传 prompt 作为位置参数，stdin=DEVNULL 防止挂起）
        request_id = uuid.uuid4().hex[:24]
        t0 = time.time()

        try:
            result = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=600,
            )
        except FileNotFoundError:
            self._send_json(500, {"error": {"message": "Claude CLI not found", "type": "server_error"}})
            return
        except subprocess.TimeoutExpired:
            self._send_json(504, {"error": {"message": f"Claude CLI timed out ({600}s)", "type": "timeout_error"}})
            return

        elapsed = time.time() - t0

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()[:500]
            stdout = (result.stdout or "").strip()[:200]
            print(f"  ❌ CLI 退出码 {result.returncode}")
            if stderr:
                print(f"     stderr: {stderr[:300]}")
            if stdout:
                print(f"     stdout: {stdout[:300]}")
            self._send_json(500, {"error": {
                "message": f"Claude CLI error (code {result.returncode}): {stderr}",
                "type": "server_error",
            }})
            return

        text = result.stdout.strip()
        if not text:
            self._send_json(500, {"error": {"message": "Claude CLI returned empty response", "type": "server_error"}})
            return

        # 如果请求了 JSON schema，尝试清理响应中的 markdown 围栏
        if schema:
            text = self._extract_json_text(text)

        print(f"  ✅ 响应 {len(text)} 字符，耗时 {elapsed:.1f}s")

        # 返回 OpenAI 格式
        self._send_json(200, {
            "id": f"chatcmpl-{request_id}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model_raw,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": len(prompt) // 4,
                "completion_tokens": len(text) // 4,
                "total_tokens": (len(prompt) + len(text)) // 4,
            },
        })

    @staticmethod
    def _extract_json_schema(body: dict) -> dict | None:
        """从 OpenAI response_format 中提取 JSON Schema。

        支持两种格式：
        1. OpenAI 格式: response_format.json_schema.schema
        2. 简化格式: response_format.schema
        """
        rf = body.get("response_format")
        if not rf or not isinstance(rf, dict):
            return None

        # OpenAI 格式: {"type": "json_schema", "json_schema": {"name": "...", "schema": {...}}}
        if rf.get("type") == "json_schema":
            js = rf.get("json_schema", {})
            if isinstance(js, dict) and "schema" in js:
                return js["schema"]

        # 简化格式: {"type": "json_schema", "schema": {...}}
        if "schema" in rf:
            return rf["schema"]

        return None

    @staticmethod
    def _extract_json_text(text: str) -> str:
        """从 LLM 响应中提取纯 JSON，去除 markdown 围栏等包装。"""
        # 去除 ```json ... ``` 围栏
        m = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
        if m:
            return m.group(1).strip()
        # 尝试找到第一个 { 或 [ 开头的 JSON
        for i, ch in enumerate(text):
            if ch in ('{', '['):
                return text[i:].strip()
        return text

    @staticmethod
    def _build_prompt(messages: list) -> str:
        """将 OpenAI messages 格式转为单一 prompt。"""
        parts = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            # 处理 content 为数组的情况
            if isinstance(content, list):
                content = "\n".join(
                    p.get("text", "") for p in content if p.get("type") == "text"
                )
            if role == "system":
                parts.append(f"<system>\n{content}\n</system>\n")
            elif role == "assistant":
                parts.append(f"<assistant>\n{content}\n</assistant>\n")
            else:
                parts.append(content)
        return "\n".join(parts).strip()


def main():
    # 启动前检查 Claude CLI
    print("Claude Max API Proxy (Python)")
    print("=" * 40)
    print()

    print("检查 Claude CLI...")
    try:
        ver = subprocess.run(
            ["claude", "--version"], capture_output=True, text=True, timeout=10
        )
        version = ver.stdout.strip() or "unknown"
        print(f"  Claude CLI: {version}")
    except FileNotFoundError:
        print("  ❌ 未找到 claude 命令，请先安装: npm install -g @anthropic-ai/claude-code")
        sys.exit(1)

    print()
    print(f"[Server] 代理运行在 http://127.0.0.1:{PORT}")
    print(f"[Server] OpenAI 兼容端点: http://127.0.0.1:{PORT}/v1/chat/completions")
    print(f"[Server] 支持 response_format.json_schema → --json-schema 透传")
    print()
    print("按 Ctrl+C 停止。")
    print()

    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

    server = ThreadingHTTPServer(("127.0.0.1", PORT), ProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭...")
        server.server_close()
        print("已停止。")


if __name__ == "__main__":
    main()

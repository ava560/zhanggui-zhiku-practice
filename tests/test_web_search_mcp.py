"""百炼 MCP 搜索节点的协议边界、结果整理和 JSON 输出。"""

import asyncio
import json
import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import AsyncMock, patch


class WebSearchMcpTests(TestCase):
    def test_new_sdk_schema_does_not_read_deprecated_input_schema_alias(self):
        from search.node_web_search_mcp import _tool_arguments

        class NewSdkTool:
            name = "search_pro"
            input_schema = {"properties": {"query": {"type": "string"}}}

            @property
            def inputSchema(self):
                raise AssertionError("不得读取已弃用的 inputSchema")

        self.assertEqual(_tool_arguments(NewSdkTool(), "产品 A 如何重启？"), {"query": "产品 A 如何重启？"})

    def test_streamable_http_uses_configured_url_key_and_discovered_search_tool(self):
        from search.node_web_search_mcp import search_web

        seen = {}

        class FakeTransport:
            def __init__(self, url, headers):
                seen["url"] = url
                seen["authorization"] = headers["Authorization"]

        class FakeClient:
            def __init__(self, transport):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                pass

            async def list_tools(self):
                return [SimpleNamespace(
                    name="search_pro",
                    inputSchema={"properties": {"query": {"type": "string"}, "count": {"type": "integer"}}},
                )]

            async def call_tool(self, name, arguments):
                seen["tool"] = name
                seen["arguments"] = arguments
                return SimpleNamespace(
                    is_error=False,
                    data={"results": [{"title": "官网", "url": "https://example.com/a", "snippet": "使用说明"}]},
                    content=[],
                )

        fastmcp = types.ModuleType("fastmcp")
        fastmcp.Client = FakeClient
        client_package = types.ModuleType("fastmcp.client")
        transports = types.ModuleType("fastmcp.client.transports")
        transports.StreamableHttpTransport = FakeTransport
        with (
            patch.dict(sys.modules, {
                "fastmcp": fastmcp,
                "fastmcp.client": client_package,
                "fastmcp.client.transports": transports,
            }),
            patch.dict(os.environ, {
                "MCP_DASHSCOPE_BASE_URL": "https://example.com/mcp",
                "MCP_DASHSCOPE_API_KEY": "test-key",
            }),
        ):
            results = asyncio.run(search_web("产品 A 如何重启？"))

        self.assertEqual(seen["url"], "https://example.com/mcp")
        self.assertEqual(seen["authorization"], "Bearer test-key")
        self.assertEqual(seen["tool"], "search_pro")
        self.assertEqual(seen["arguments"], {"query": "产品 A 如何重启？", "count": 5})
        self.assertEqual(results, [{"title": "官网", "url": "https://example.com/a", "content": "使用说明"}])

    def test_mcp_text_json_deduplicates_urls_and_limits_to_five(self):
        from search.node_web_search_mcp import parse_search_results

        rows = [
            {"title": f"结果{i}", "link": f"https://example.com/{i}", "summary": f"摘要{i}"}
            for i in range(7)
        ]
        rows.insert(2, {"title": "重复", "link": "https://example.com/0", "summary": "重复摘要"})
        response = SimpleNamespace(
            is_error=False,
            data=None,
            content=[SimpleNamespace(type="text", text=json.dumps({"data": {"items": rows}}, ensure_ascii=False))],
        )

        results = parse_search_results(response)

        self.assertEqual(len(results), 5)
        self.assertEqual([item["url"] for item in results], [f"https://example.com/{i}" for i in range(5)])
        self.assertEqual(results[0], {"title": "结果0", "url": "https://example.com/0", "content": "摘要0"})

    def test_prefers_summary_when_mcp_also_returns_full_page_content(self):
        from search.node_web_search_mcp import parse_search_results

        response = SimpleNamespace(
            is_error=False,
            data={"results": [{
                "title": "产品文档",
                "url": "https://example.com/doc",
                "content": "很长的网页正文",
                "summary": "简短摘要",
            }]},
            content=[],
        )

        self.assertEqual(parse_search_results(response)[0]["content"], "简短摘要")

    def test_node_saves_top5_json_and_state_from_rewritten_question(self):
        from search.node_web_search_mcp import node_web_search_mcp

        state = {
            "question_rewritten": "产品 A 如何重启？",
            "conversation_id": "conversation-1",
            "local_dir": str(Path.cwd() / "test-output"),
        }
        rows = [{"title": "官网", "url": "https://example.com/a", "content": "使用说明"}]
        with (
            patch("search.node_web_search_mcp.search_web", new_callable=AsyncMock, return_value=rows) as search,
            patch.object(Path, "mkdir"),
            patch.object(Path, "write_text") as write_json,
        ):
            result = node_web_search_mcp(state)

        search.assert_awaited_once_with("产品 A 如何重启？")
        self.assertEqual(result["web_search_top5"], rows)
        self.assertEqual(json.loads(write_json.call_args.args[0]), {
            "question_rewritten": "产品 A 如何重启？",
            "top5": rows,
        })
        self.assertTrue(result["web_search_json_path"].endswith("_web_search_mcp_top5.json"))

    def test_blank_question_is_rejected_before_network_call(self):
        from search.node_web_search_mcp import node_web_search_mcp

        with patch("search.node_web_search_mcp.search_web", new_callable=AsyncMock) as search:
            with self.assertRaises(ValueError):
                node_web_search_mcp({"question_rewritten": "  "})
        search.assert_not_awaited()

    def test_node_runs_when_an_event_loop_is_already_running(self):
        from search.node_web_search_mcp import node_web_search_mcp

        state = {
            "question_rewritten": "产品 A 如何重启？",
            "conversation_id": "conversation-1",
            "local_dir": str(Path.cwd() / "test-output"),
        }
        rows = [{"title": "官网", "url": "https://example.com/a", "content": "使用说明"}]

        async def inside_running_loop():
            with (
                patch("search.node_web_search_mcp.search_web", new_callable=AsyncMock, return_value=rows),
                patch.object(Path, "mkdir"),
                patch.object(Path, "write_text"),
            ):
                return node_web_search_mcp(state)

        result = asyncio.run(inside_running_loop())
        self.assertEqual(result["web_search_top5"], rows)
        self.assertFalse(result.get("web_search_error"))

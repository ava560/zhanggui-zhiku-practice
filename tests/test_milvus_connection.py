"""Milvus 连接不应被进程中无关的 HTTP 代理劫持。"""

import os
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch, sentinel

from upload import node_import_milvus, node_item_name_recognition


class MilvusConnectionTests(TestCase):
    def test_both_nodes_connect_directly_by_default(self) -> None:
        settings = SimpleNamespace(milvus_url="https://milvus.example", milvus_token="token")
        for node in (node_import_milvus, node_item_name_recognition):
            with self.subTest(node=node.__name__):
                with patch.dict(os.environ, {"MILVUS_USE_PROXY": ""}), patch(
                    "pymilvus.MilvusClient", return_value=sentinel.client
                ) as client:
                    result = node.get_milvus_client(settings)

                self.assertIs(result, sentinel.client)
                self.assertEqual(
                    client.call_args.kwargs["grpc_options"],
                    {"grpc.enable_http_proxy": 0},
                )

    def test_proxy_can_be_enabled_for_networks_that_require_it(self) -> None:
        settings = SimpleNamespace(milvus_url="https://milvus.example", milvus_token="token")
        with patch.dict(os.environ, {"MILVUS_USE_PROXY": "1"}):
            for node in (node_import_milvus, node_item_name_recognition):
                with self.subTest(node=node.__name__):
                    with patch("pymilvus.MilvusClient") as client:
                        node.get_milvus_client(settings)
                    self.assertNotIn("grpc_options", client.call_args.kwargs)

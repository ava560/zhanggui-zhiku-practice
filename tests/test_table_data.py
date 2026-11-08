"""元数据、表数据和每日任务连到本机 PostgreSQL。样本不用需求说明里的示例公司。"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from unittest import TestCase

from structured.catalog import (
    confirm_and_publish,
    register_document,
    replace_rows,
    stage_table,
)
from structured.indexes import RecordingIndex
from structured.jobs import delete_job, preview_job, run_job, save_job, update_job
from structured.objects import MemoryStore
from structured.query_table import query_table_data
from structured.session import open_db


def _csv(headers: list[str], rows: list[list[object]]) -> bytes:
    lines = [",".join(headers)]
    lines.extend(",".join("" if value is None else str(value) for value in row) for row in rows)
    return ("\n".join(lines) + "\n").encode("utf-8")


class PostgresTableTests(TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.conn = open_db()
        cls.prefix = "beizhu_" + uuid.uuid4().hex[:8]
        cls._wipe("beizhu_%")

    @classmethod
    def _wipe(cls, pattern: str) -> None:
        docs = cls.conn.execute(
            "SELECT doc_id FROM doc_info WHERE source_file LIKE %s",
            (pattern,),
        ).fetchall()
        doc_ids = [row["doc_id"] for row in docs]
        if not doc_ids:
            cls.conn.rollback()
            return
        tables = cls.conn.execute(
            "SELECT DISTINCT table_name FROM table_info WHERE doc_id = ANY(%s)",
            (doc_ids,),
        ).fetchall()
        versions = cls.conn.execute(
            "SELECT import_version_id FROM doc_info WHERE doc_id = ANY(%s)",
            (doc_ids,),
        ).fetchall()
        version_ids = [row["import_version_id"] for row in versions]
        cls.conn.execute("DELETE FROM data_job WHERE doc_ids && %s", (doc_ids,))
        if version_ids:
            cls.conn.execute("DELETE FROM metric_info WHERE import_version_id = ANY(%s)", (version_ids,))
            cls.conn.execute("DELETE FROM column_info WHERE import_version_id = ANY(%s)", (version_ids,))
            cls.conn.execute("DELETE FROM table_info WHERE import_version_id = ANY(%s)", (version_ids,))
            cls.conn.execute("DELETE FROM doc_info WHERE import_version_id = ANY(%s)", (version_ids,))
        for table in tables:
            cls.conn.execute(f'DROP TABLE IF EXISTS "{table["table_name"]}"')
        cls.conn.commit()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._wipe(cls.prefix + "%")
        cls.conn.close()

    def test_lookup_keeps_uncommitted_work_on_the_same_connection(self) -> None:
        try:
            self.conn.execute("CREATE TEMP TABLE beizhu_probe (id integer)")
            self.conn.execute("INSERT INTO beizhu_probe (id) VALUES (1)")
            query_table_data("北渚有没有记录", conn=self.conn, indexes=RecordingIndex(), plan={"table_id": "missing"})
            found = self.conn.execute("SELECT id FROM beizhu_probe").fetchone()
            self.assertEqual(found["id"], 1)
        finally:
            self.conn.rollback()

    def test_confirmation_is_required_and_lookup_uses_the_real_table(self) -> None:
        filename = f"{self.prefix}_申购.csv"
        draft = self._stage(filename, ["产品", "申购金额"], [["北渚_180", "1200"], ["北渚30", "300"]])
        self.assertEqual(draft["status"], "pending")
        hidden = query_table_data("北渚申购金额", conn=self.conn, plan={"table_id": draft["table_id"], "columns": ["c1"]})
        self.assertEqual(hidden["table_reason"], "metadata_missing")
        published = self._publish(draft, {"申购金额": "度量"})
        found = query_table_data(
            "北渚的申购金额",
            conn=self.conn,
            plan={
                "table_id": published["table_id"],
                "columns": ["c1", "c2"],
                "filters": [{"column": "c1", "op": "contains", "value": "北渚_180"}],
            },
        )
        self.assertEqual(found["table_reason"], "")
        result = found["table_results"][0]
        self.assertEqual(result["source_file"], filename)
        self.assertEqual(result["table_name"], published["table_name"])
        self.assertIn("c2", result["columns"])
        self.assertEqual(result["rows"], [{"c1": "北渚_180", "c2": 1200}])
        self.assertNotIn(self.prefix + "_另一份", json.dumps(found, ensure_ascii=False))

    def test_description_number_is_not_a_row_and_extra_field_keeps_old_rows(self) -> None:
        filename = f"{self.prefix}_空表.csv"
        draft = self._stage(filename, ["网点"], [])
        draft["columns"][0]["description"] = "示例网点规模 999"
        published = self._publish(draft, {})
        empty = query_table_data("网点", conn=self.conn, plan={"table_id": published["table_id"], "columns": ["c1"]})
        self.assertEqual(empty["table_reason"], "no_records")
        self.assertNotIn("999", json.dumps(empty, ensure_ascii=False))
        with self.assertRaises(Exception):
            replace_rows(self.conn, RecordingIndex(), published["doc_id"], [{"c1": "甲", "多余": "1"}])
        current = self.conn.execute(
            "SELECT data_version_id FROM doc_info WHERE import_version_id = %s",
            (published["import_version_id"],),
        ).fetchone()
        self.assertEqual(current["data_version_id"], published["data_version_id"])

    def test_restricted_formula_rejects_free_sql_cross_table_and_attribute_sum(self) -> None:
        filename = f"{self.prefix}_订单.csv"
        draft = self._stage(filename, ["金额", "订单号", "备注"], [["80", "A1", "北区"], ["40", "", "北区"]])
        amount, order, note = [column["column_id"] for column in draft["columns"]]
        published = self._publish(
            draft,
            {"金额": "度量", "订单号": "属性", "备注": "属性"},
            metrics=[{
                "name": "客单额",
                "status": "approved",
                "column_ids": [amount, order],
                "formula_text": "金额合计 / 订单数",
                "formula_tree": {
                    "op": "div",
                    "left": {"op": "sum", "column_id": amount},
                    "right": {"op": "count_distinct", "column_id": order},
                },
            }],
        )
        metric_id = self.conn.execute(
            "SELECT metric_id, formula_tree FROM metric_info WHERE import_version_id = %s",
            (published["import_version_id"],),
        ).fetchone()
        self.assertNotIn("where", json.dumps(metric_id["formula_tree"]))
        self.assertNotIn("group", json.dumps(metric_id["formula_tree"]))
        counted = query_table_data(
            "客单额",
            conn=self.conn,
            plan={"table_id": published["table_id"], "metric_id": metric_id["metric_id"], "filters": [{"column": "c3", "op": "eq", "value": "北区"}]},
        )
        self.assertEqual(float(counted["table_results"][0]["rows"][0]["value"]), 120)
        zero = self._stage(f"{self.prefix}_零分母.csv", ["金额", "订单号"], [["10", ""]])
        amount_id, order_id = [column["column_id"] for column in zero["columns"]]
        zero_published = self._publish(
            zero,
            {"金额": "度量"},
            metrics=[{
                "name": "空客单",
                "status": "approved",
                "column_ids": [amount_id, order_id],
                "formula_tree": {
                    "op": "div",
                    "left": {"op": "sum", "column_id": amount_id},
                    "right": {"op": "count_distinct", "column_id": order_id},
                },
            }],
        )
        zero_metric = self.conn.execute(
            "SELECT metric_id FROM metric_info WHERE import_version_id = %s",
            (zero_published["import_version_id"],),
        ).fetchone()
        divided = query_table_data(
            "空客单",
            conn=self.conn,
            plan={"table_id": zero_published["table_id"], "metric_id": zero_metric["metric_id"]},
        )
        self.assertEqual(divided["table_reason"], "no_records")
        self.assertIn("除数为零", divided["detail"])
        self.assertNotEqual(json.dumps(divided), json.dumps({"value": 0}))
        rejected = query_table_data("备注", conn=self.conn, plan={"sql": "select 1", "table_id": published["table_id"]})
        self.assertEqual(rejected["table_reason"], "metric_unconfirmed")
        attribute_sum = query_table_data(
            "备注",
            conn=self.conn,
            plan={"table_id": published["table_id"], "aggregates": [{"agg": "sum", "column_id": note, "as": "bad_sum"}]},
        )
        self.assertEqual(attribute_sum["table_reason"], "metric_unconfirmed")
        draft_metric = self._stage(f"{self.prefix}_草稿.csv", ["金额"], [["5"]])
        draft_published = self._publish(
            draft_metric,
            {"金额": "度量"},
            metrics=[{
                "name": "未确认金额",
                "status": "draft",
                "column_ids": [draft_metric["columns"][0]["column_id"]],
                "formula_tree": {"op": "sum", "column_id": draft_metric["columns"][0]["column_id"]},
            }],
        )
        unconfirmed = query_table_data(
            "未确认金额",
            conn=self.conn,
            plan={"table_id": draft_published["table_id"], "metric_id": self.conn.execute(
                "SELECT metric_id FROM metric_info WHERE import_version_id = %s",
                (draft_published["import_version_id"],),
            ).fetchone()["metric_id"]},
        )
        self.assertEqual(unconfirmed["table_reason"], "metric_unconfirmed")
        self.assertEqual(unconfirmed["table_results"], [])

    def test_versions_duplicates_index_failure_and_documents(self) -> None:
        filename = f"{self.prefix}_版本.csv"
        store = MemoryStore()
        indexes = RecordingIndex()
        first = stage_table(self.conn, store, filename, _csv(["区域"], [["东"]]))
        published = self._publish(first, {}, store=store, indexes=indexes)
        duplicate = stage_table(self.conn, store, filename, _csv(["区域"], [["东"]]))
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(len(store.objects), 1)
        revised = stage_table(self.conn, store, filename, _csv(["区域"], [["西"]]))
        self.assertFalse(revised["duplicate"])
        blocked = RecordingIndex(fail=True)
        with self.assertRaises(Exception):
            self._publish(revised, {}, store=store, indexes=blocked)
        ready = self.conn.execute(
            "SELECT import_version_id, data_version_id FROM doc_info WHERE doc_id = %s AND status = 'ready'",
            (published["doc_id"],),
        ).fetchone()
        self.assertEqual(ready["import_version_id"], published["import_version_id"])
        visible = query_table_data("区域", conn=self.conn, plan={"table_id": published["table_id"], "columns": ["c1"]})
        self.assertEqual(visible["table_results"][0]["rows"], [{"c1": "东"}])
        second = self._publish(revised, {}, store=store, indexes=RecordingIndex())
        visible = query_table_data("区域", conn=self.conn, plan={"table_id": second["table_id"], "columns": ["c1"]})
        self.assertEqual([row["c1"] for row in visible["table_results"][0]["rows"]], ["西"])
        kept = self.conn.execute(
            "SELECT count(*) AS n FROM doc_info WHERE doc_id = %s",
            (published["doc_id"],),
        ).fetchone()
        self.assertGreaterEqual(kept["n"], 2)
        document = register_document(self.conn, MemoryStore(), f"{self.prefix}_说明.pdf", b"%PDF-beizhu", summary="北渚说明书")
        tables = self.conn.execute(
            "SELECT 1 FROM table_info WHERE doc_id = %s",
            (document["doc_id"],),
        ).fetchall()
        self.assertEqual(tables, [])

    def test_two_files_are_not_joined_and_database_failure_is_not_no_records(self) -> None:
        north = self._stage(f"{self.prefix}_北渚申购.csv", ["北渚申购"], [["100"]])
        south = self._stage(f"{self.prefix}_澄湾赎回.csv", ["澄湾赎回"], [["40"]])
        self._publish(north, {"北渚申购": "度量"})
        self._publish(south, {"澄湾赎回": "度量"})
        mixed = query_table_data("把北渚申购和澄湾赎回合并", conn=self.conn)
        self.assertTrue(any("没有登记表间关联" in gap for gap in mixed["gaps"]))
        self.assertEqual(len(mixed["table_results"]), 1)
        closed = open_db()
        closed.close()
        failed = query_table_data("北渚申购", conn=closed)
        self.assertEqual(failed["table_reason"], "query_failed")
        self.assertNotEqual(failed["table_reason"], "no_records")

    def test_daily_job_runs_only_its_code_and_chat_does_not(self) -> None:
        filename = f"{self.prefix}_任务.csv"
        draft = self._stage(filename, ["区域", "金额"], [["东", "10"]])
        published = self._publish(draft, {"金额": "度量", "区域": "维度"}, value_index={"区域": True})
        indexes = RecordingIndex()
        calls = []

        def renew(context) -> None:
            calls.append(context.doc_id)
            context.replace_rows([{"c1": "西", "c2": 20}])

        codes = {"renew_beizhu": {"fn": renew, "doc_id": published["doc_id"]}}
        missing = save_job(
            self.conn,
            name=f"{self.prefix} 无代码",
            sop_ref="SOP-北渚-空",
            run_at="09:30",
            doc_ids=[published["doc_id"]],
        )
        untouched = run_job(self.conn, missing["job_id"], codes, indexes, today=datetime(2026, 11, 8).date())
        self.assertEqual(untouched["last_status"], "never")
        self.assertEqual(calls, [])
        job = save_job(
            self.conn,
            name=f"{self.prefix} 更新",
            sop_ref="SOP-北渚",
            run_at="09:30",
            doc_ids=[published["doc_id"]],
            code_id="renew_beizhu",
            description="只更新北渚",
        )
        edited = update_job(self.conn, job["job_id"], description="改描述", run_at="10:15")
        self.assertEqual(edited["description"], "改描述")
        self.assertEqual(edited["last_status"], "never")
        done = run_job(self.conn, job["job_id"], codes, indexes, today=datetime(2026, 11, 8).date())
        self.assertEqual(done["last_status"], "succeeded")
        self.assertEqual(calls, [published["doc_id"]])
        self.assertTrue(any(call["kind"] == "values" for call in indexes.calls))
        again = run_job(self.conn, job["job_id"], codes, indexes, today=datetime(2026, 11, 8).date())
        self.assertEqual(len(calls), 1)
        self.assertEqual(again["last_success_on"], "2026-11-08")
        preview = preview_job(self.conn, job["job_id"], codes)
        self.assertIn("replace_rows", preview["code_preview"])
        self.assertEqual(preview["description"], "改描述")
        self.assertIn(filename, json.dumps(preview["files"], ensure_ascii=False))

        def explode(context) -> None:
            raise RuntimeError("北渚更新中断")

        broken = save_job(
            self.conn,
            name=f"{self.prefix} 失败",
            sop_ref="SOP-北渚-失败",
            run_at="11:00",
            doc_ids=[published["doc_id"]],
            code_id="explode_beizhu",
        )
        before = self.conn.execute(
            "SELECT data_version_id FROM doc_info WHERE doc_id = %s AND status = 'ready'",
            (published["doc_id"],),
        ).fetchone()
        failed = run_job(
            self.conn,
            broken["job_id"],
            {"explode_beizhu": {"fn": explode, "doc_id": published["doc_id"]}},
            RecordingIndex(),
            today=datetime(2026, 11, 9).date(),
        )
        after = self.conn.execute(
            "SELECT data_version_id FROM doc_info WHERE doc_id = %s AND status = 'ready'",
            (published["doc_id"],),
        ).fetchone()
        self.assertEqual(failed["last_status"], "failed")
        self.assertEqual(before["data_version_id"], after["data_version_id"])
        blocked = RecordingIndex(fail=True)
        with self.assertRaises(Exception):
            replace_rows(self.conn, blocked, published["doc_id"], [{"c1": "南", "c2": 1}])
        still = self.conn.execute(
            "SELECT data_version_id FROM doc_info WHERE doc_id = %s AND status = 'ready'",
            (published["doc_id"],),
        ).fetchone()
        self.assertEqual(still["data_version_id"], after["data_version_id"])
        from search.agent_workflow import run_agent_workflow
        from search.long_term_memory import LongTermMemoryBook

        answered = run_agent_workflow(
            {"current_question": "请立即更新北渚的表", "user_id": "beizhu-user"},
            llm=lambda _prompt: '{"intent":"chat"}',
            ports={"prepare": lambda _state: {}},
            memory=LongTermMemoryBook(),
        )
        self.assertEqual(answered["agent_intent"], "chat")
        unchanged = self.conn.execute(
            "SELECT data_version_id FROM doc_info WHERE doc_id = %s AND status = 'ready'",
            (published["doc_id"],),
        ).fetchone()
        self.assertEqual(unchanged["data_version_id"], still["data_version_id"])
        delete_job(self.conn, job["job_id"])
        self.assertEqual(
            self.conn.execute("SELECT 1 FROM data_job WHERE job_id = %s", (job["job_id"],)).fetchone(),
            None,
        )

    def _stage(self, filename: str, headers: list[str], rows: list[list[object]]) -> dict:
        store = MemoryStore()
        draft = stage_table(self.conn, store, filename, _csv(headers, rows))
        draft["_store"] = store
        return draft

    def _publish(self, draft: dict, roles: dict[str, str], metrics=None, store=None, indexes=None, value_index=None) -> dict:
        value_index = value_index or {}
        confirmation = {
            "summary": "北渚测试文件",
            "table_role": "事实表",
            "table_summary": "北渚业务表",
            "columns": [
                {
                    **column,
                    "role": roles.get(column["aliases"][0], column["role"]),
                    "description": column["description"],
                    "value_index": value_index.get(column["aliases"][0], False),
                }
                for column in draft["columns"]
            ],
            "metrics": metrics or [],
        }
        published = confirm_and_publish(
            self.conn,
            store or draft["_store"],
            indexes or RecordingIndex(),
            draft["import_version_id"],
            confirmation,
        )
        published["table_id"] = draft["table_id"]
        return published

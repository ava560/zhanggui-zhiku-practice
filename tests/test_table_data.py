"""元数据、表数据和每日任务连到本机 PostgreSQL。样本不用需求说明里的示例公司。"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
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
        skill_table = cls.conn.execute("SELECT to_regclass('public.user_skill') AS name").fetchone()
        if skill_table and skill_table["name"]:
            cls.conn.execute("DELETE FROM user_skill WHERE name LIKE %s", (pattern,))
        docs = cls.conn.execute(
            "SELECT doc_id FROM doc_info WHERE source_file LIKE %s",
            (pattern,),
        ).fetchall()
        doc_ids = [row["doc_id"] for row in docs]
        if not doc_ids:
            cls.conn.commit()
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
        if cls.conn.execute("SELECT to_regclass('public.code_version') AS name").fetchone()["name"]:
            cls.conn.execute("DELETE FROM code_version WHERE name LIKE %s", (pattern,))
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

    def test_weekly_job_does_not_run_on_another_weekday(self) -> None:
        from structured.jobs import _is_due

        filename = f"{self.prefix}_周期.csv"
        published = self._publish(self._stage(filename, ["区域"], [["东"]]), {"区域": "维度"})
        calls = []

        def renew(context) -> None:
            calls.append(context.doc_id)

        codes = {"renew_week": {"fn": renew, "doc_id": published["doc_id"]}}
        monday_job = save_job(
            self.conn,
            name=f"{self.prefix} 周一",
            sop_ref="SOP-周一",
            run_at="09:30",
            doc_ids=[published["doc_id"]],
            code_id="renew_week",
            schedule_kind="weekly",
            schedule_day=1,
        )
        month_job = save_job(
            self.conn,
            name=f"{self.prefix} 每月",
            sop_ref="SOP-每月",
            run_at="09:30",
            doc_ids=[published["doc_id"]],
            code_id="renew_week",
            schedule_kind="monthly",
            schedule_day=9,
        )
        self.assertEqual(monday_job["schedule_kind"], "weekly")
        self.assertEqual(monday_job["schedule_day"], 1)
        tuesday = datetime(2026, 11, 10, 9, 30)
        monday = datetime(2026, 11, 9, 9, 30)
        self.assertFalse(_is_due(monday_job, tuesday))
        self.assertTrue(_is_due(monday_job, monday))
        self.assertTrue(_is_due(month_job, monday))
        self.assertFalse(_is_due(month_job, tuesday))
        done = run_job(self.conn, monday_job["job_id"], codes, RecordingIndex(), today=monday.date())
        self.assertEqual(done["last_status"], "succeeded")
        self.assertEqual(calls, [published["doc_id"]])

    def test_new_code_version_keeps_the_old_object_and_a_failed_run_stays_pending(self) -> None:
        from structured.code_versions import confirm_code, ready_source, stage_code

        filename = f"{self.prefix}_代码表.csv"
        published = self._publish(self._stage(filename, ["区域"], [["东"]]), {"区域": "维度"})
        store = MemoryStore()
        script_name = f"{self.prefix}_更新.py"
        first = stage_code(self.conn, store, script_name, b"raise RuntimeError('beizhu broken')\n")
        ready = confirm_code(
            self.conn,
            first["version_id"],
            name=script_name,
            description="第一版",
            doc_id=published["doc_id"],
        )
        job = save_job(
            self.conn,
            name=f"{self.prefix} 代码",
            sop_ref="SOP-代码",
            run_at="08:00",
            doc_ids=[published["doc_id"]],
            code_id=ready["code_id"],
        )
        failed = run_job(
            self.conn,
            job["job_id"],
            {},
            RecordingIndex(),
            today=datetime(2026, 11, 8).date(),
            store=store,
            fixer=lambda _source, _error: "context.replace_rows([{'c1': '南'}])\n",
        )
        self.assertEqual(failed["last_status"], "failed")
        self.assertIn("待确认修改", failed["last_error"])
        self.assertIn("没有替换正在使用的版本", failed["last_error"])
        still_ready, _doc_id = ready_source(self.conn, store, ready["code_id"])
        self.assertIn("beizhu broken", still_ready)
        second = stage_code(
            self.conn,
            store,
            script_name,
            "context.replace_rows([{'c1': '西'}])\n".encode(),
            code_id=ready["code_id"],
        )
        confirm_code(
            self.conn,
            second["version_id"],
            name=script_name,
            description="第二版",
            doc_id=published["doc_id"],
        )
        self.assertIn(first["minio_key"], store.objects)
        archived = self.conn.execute(
            "SELECT status FROM code_version WHERE version_id = %s",
            (first["version_id"],),
        ).fetchone()
        self.assertEqual(archived["status"], "archived")
        current, _doc_id = ready_source(self.conn, store, ready["code_id"])
        self.assertIn("西", current)
        pending = self.conn.execute(
            "SELECT status FROM code_version WHERE code_id = %s AND status = 'pending'",
            (ready["code_id"],),
        ).fetchall()
        self.assertEqual(len(pending), 1)

    def test_unchanged_values_still_move_the_value_index(self) -> None:
        filename = f"{self.prefix}_原样.csv"
        published = self._publish(
            self._stage(filename, ["代码"], [["000001.OF"]]),
            {"代码": "属性"},
            value_index={"代码": True},
        )
        indexes = RecordingIndex()
        replaced = replace_rows(self.conn, indexes, published["doc_id"], [{"c1": "000001.OF"}])
        written = [call for call in indexes.calls if call["kind"] == "values"]
        self.assertEqual(written, [])
        self.assertNotIn("delete_values", [call["kind"] for call in indexes.calls])
        self.assertNotEqual(replaced["data_version_id"], published["data_version_id"])

    def test_user_defined_skill_is_saved_and_applied(self) -> None:
        from structured.skills import apply_skill, delete_skill, list_skills, save_skill

        saved = save_skill(
            self.conn,
            name=self.prefix + "查表",
            summary="先看已发布的表",
            instructions="只回答表里已经发布的记录。",
        )
        try:
            self.assertTrue(saved["skill_id"].startswith("sk_"))
            found = [item for item in list_skills(self.conn) if item["skill_id"] == saved["skill_id"]]
            self.assertEqual(found[0]["summary"], "先看已发布的表")
            resolved = apply_skill(saved["skill_id"], "有几行")
            self.assertEqual(resolved["question"], "有几行")
            self.assertEqual(resolved["instructions"], "只回答表里已经发布的记录。")
            self.assertFalse(resolved["stop"])
            with self.assertRaises(ValueError):
                apply_skill("sk_" + "0" * 32, "有几行")
        finally:
            delete_skill(self.conn, saved["skill_id"])

    def test_skill_mention_keeps_the_question_and_unknown_names_stop(self) -> None:
        from structured.skills import resolve_turn, save_skill, delete_skill

        saved = save_skill(
            self.conn,
            name="追加业绩行",
            summary="给已发布的业绩表追加一行时使用",
            instructions="先核对列是否与已发布表一致，再追加一行，然后根据这次实际读到的输入记下血缘。",
        )
        try:
            hit = resolve_turn("@追加业绩行 再加一行")
            self.assertEqual(hit["question"], "@追加业绩行 再加一行")
            self.assertIn("核对列", hit["instructions"])
            self.assertFalse(hit["stop"])
            missed = resolve_turn("@没有这个技能 昨天卖了多少")
            self.assertTrue(missed["stop"])
            self.assertIn("未找到", missed["message"])
        finally:
            delete_skill(self.conn, saved["skill_id"])

    def test_skill_code_does_not_run_the_job(self) -> None:
        from structured.skills import delete_skill, save_skill

        filename = f"{self.prefix}_技能任务.csv"
        published = self._publish(self._stage(filename, ["区域"], [["东"]]), {"区域": "维度"})
        calls = []

        def renew(context) -> None:
            calls.append(context.doc_id)

        saved = save_skill(self.conn, name=self.prefix + "技能", instructions="按步骤更新。", code_id="renew_skill_only")
        try:
            job = save_job(
                self.conn,
                name=f"{self.prefix} 只绑技能",
                sop_ref="SOP-技能",
                run_at="09:30",
                doc_ids=[published["doc_id"]],
                skill_id=saved["skill_id"],
            )
            done = run_job(
                self.conn,
                job["job_id"],
                {"renew_skill_only": {"fn": renew, "doc_id": published["doc_id"]}},
                RecordingIndex(),
                today=datetime(2026, 11, 9).date(),
            )
            self.assertEqual(done["last_status"], "never")
            self.assertEqual(calls, [])
        finally:
            delete_skill(self.conn, saved["skill_id"])

    def test_library_folder_can_hold_a_moved_file(self) -> None:
        from structured.library import create_folder, list_library, move_item, rename_folder

        filename = f"{self.prefix}_目录.csv"
        published = self._publish(self._stage(filename, ["区域"], [["东"]]), {"区域": "维度"})
        folder = create_folder(self.conn, name=f"{self.prefix}资料夹")
        try:
            renamed = rename_folder(self.conn, folder["folder_id"], f"{self.prefix}已改名")
            root = list_library(self.conn, uploads=[])
            folder_row = next(item for item in root["items"] if item["id"] == folder["folder_id"])
            self.assertEqual(renamed["name"], f"{self.prefix}已改名")
            self.assertEqual(folder_row["name"], f"{self.prefix}已改名")
            self.assertEqual(folder_row["type"], "文件夹")
            self.assertTrue(folder_row["modified"])
            file_row = next(item for item in root["items"] if item["id"] == published["doc_id"])
            self.assertEqual(file_row["type"], "CSV 文件")
            self.assertIn(published["doc_id"], [item["id"] for item in root["items"] if item["kind"] == "file"])
            move_item(self.conn, "file", published["doc_id"], folder["folder_id"], uploads=[])
            inside = list_library(self.conn, folder["folder_id"], uploads=[])
            self.assertEqual([item["id"] for item in inside["items"] if item["kind"] == "file"], [published["doc_id"]])
            root_again = list_library(self.conn, uploads=[])
            self.assertNotIn(published["doc_id"], [item["id"] for item in root_again["items"] if item["kind"] == "file"])
            nested = create_folder(self.conn, name=f"{self.prefix}下级", parent_id=folder["folder_id"])
            with self.assertRaises(Exception):
                move_item(self.conn, "folder", folder["folder_id"], nested["folder_id"])
        finally:
            self.conn.execute("DELETE FROM library_placement WHERE item_id = %s", (published["doc_id"],))
            self.conn.execute("DELETE FROM library_folder WHERE name LIKE %s", (self.prefix + "%",))
            self.conn.commit()

    def test_delete_removes_the_original_and_vectors_and_lists_tables(self) -> None:
        from pathlib import Path
        import tempfile

        from structured.indexes import RecordingIndex
        from structured.jobs import delete_job, save_job
        from structured.library import create_folder
        from structured.removal import list_tables, remove_library_item

        filename = f"{self.prefix}_删除.csv"
        draft = self._stage(filename, ["区域"], [["东"]])
        store = draft["_store"]
        indexes = RecordingIndex()
        published = self._publish(draft, {"区域": "维度"}, store=store, indexes=indexes)
        listed = list_tables(self.conn)
        metadata = [item["table_name"] for item in listed["tables"] if item["kind"] == "metadata"]
        self.assertEqual(metadata, ["doc_info", "table_info", "column_info", "metric_info"])
        shown = next(item for item in listed["tables"] if item["doc_id"] == published["doc_id"])
        self.assertEqual(shown["source_file"], filename)
        self.assertEqual(shown["row_count"], 1)
        self.assertEqual(shown["columns"][0]["name"], "c1")
        self.assertEqual(shown["columns"][0]["label"], "区域")
        self.assertEqual(shown["rows"], [{"区域": "东"}])
        key = self.conn.execute(
            "SELECT minio_key FROM doc_info WHERE doc_id = %s AND status = 'ready'",
            (published["doc_id"],),
        ).fetchone()["minio_key"]
        self.assertIn(key, store.objects)
        job = save_job(
            self.conn,
            name=f"{self.prefix} 占用文件",
            sop_ref="SOP-删除",
            run_at="09:30",
            doc_ids=[published["doc_id"]],
        )
        removed_vectors: list[str] = []
        removed_names: list[str] = []
        try:
            with self.assertRaises(Exception) as blocked:
                remove_library_item(
                    self.conn,
                    "file",
                    published["doc_id"],
                    store=store,
                    indexes=indexes,
                    milvus_delete=removed_vectors.append,
                    delete_basename=removed_names.append,
                )
            self.assertIn("还在使用", str(blocked.exception))
            self.assertEqual(removed_vectors, [])
        finally:
            delete_job(self.conn, job["job_id"])
        parent = create_folder(self.conn, name=f"{self.prefix}待删夹")
        child = create_folder(self.conn, name=f"{self.prefix}待删下级", parent_id=parent["folder_id"])
        with self.assertRaises(Exception) as occupied:
            remove_library_item(
                self.conn, "folder", parent["folder_id"], store=store, indexes=indexes, milvus_delete=removed_vectors.append,
            )
        self.assertIn("还有内容", str(occupied.exception))
        remove_library_item(
            self.conn, "folder", child["folder_id"], store=store, indexes=indexes, milvus_delete=removed_vectors.append,
        )
        copy_root = Path(tempfile.mkdtemp())
        for folder_name in ("task_a", "task_b"):
            source = copy_root / folder_name / "source"
            source.mkdir(parents=True)
            (source / filename).write_bytes(b"original")
        remove_library_item(
            self.conn,
            "file",
            published["doc_id"],
            store=store,
            indexes=indexes,
            milvus_delete=removed_vectors.append,
            delete_basename=removed_names.append,
            disk_root=copy_root,
        )
        self.assertEqual(removed_vectors, [filename])
        self.assertEqual(removed_names, [filename])
        self.assertFalse((copy_root / "task_a" / "source" / filename).exists())
        self.assertFalse((copy_root / "task_b" / "source" / filename).exists())
        self.assertNotIn(key, store.objects)
        self.assertTrue(any(call["kind"] == "delete_metadata" for call in indexes.calls))
        hidden = list_tables(self.conn)
        self.assertNotIn(published["doc_id"], [item["doc_id"] for item in hidden["tables"]])
        handle = tempfile.NamedTemporaryFile(delete=False)
        handle.close()
        upload_id = "task_delete:note.pdf"
        try:
            remove_library_item(
                self.conn,
                "upload",
                upload_id,
                store=store,
                indexes=indexes,
                milvus_delete=removed_vectors.append,
                delete_basename=removed_names.append,
                disk_path=lambda _item: Path(handle.name),
                disk_root=copy_root,
            )
        finally:
            Path(handle.name).unlink(missing_ok=True)
        self.assertEqual(removed_vectors[-1], "note.pdf")
        self.assertFalse(Path(handle.name).exists())

    def test_named_job_supplies_its_skill_when_the_user_did_not(self) -> None:
        from structured.jobs import skill_on_named_job
        from structured.skills import delete_skill, resolve_turn, save_skill

        saved = save_skill(self.conn, name=self.prefix + "追加", summary="追加一行", instructions="先核对列，再追加一行。")
        job_name = f"{self.prefix}每日追加"
        job = None
        try:
            job = save_job(
                self.conn,
                name=job_name,
                sop_ref="SOP-追加",
                run_at="09:30",
                doc_ids=[],
                skill_id=saved["skill_id"],
            )
            self.assertEqual(skill_on_named_job(self.conn, f"请处理{job_name}"), saved["skill_id"])
            resolved = resolve_turn(f"请处理{job_name}", job_skill_id=saved["skill_id"])
            self.assertEqual(resolved["question"], f"请处理{job_name}")
            self.assertIn("核对列", resolved["instructions"])
            self.assertFalse(resolved["stop"])
        finally:
            if job is not None:
                delete_job(self.conn, job["job_id"])
            delete_skill(self.conn, saved["skill_id"])

    def test_lineage_follows_the_read_version_and_a_failed_write_keeps_it(self) -> None:
        from structured.lineage import MemoryLineage

        filename = f"{self.prefix}_血缘.csv"
        published = self._publish(self._stage(filename, ["区域", "金额"], [["东", "10"]]), {"金额": "度量", "区域": "维度"})
        store = MemoryLineage()
        replaced = replace_rows(
            self.conn,
            RecordingIndex(),
            published["doc_id"],
            [{"c1": "西", "c2": 20}],
            lineage=store,
            code_id="renew_beizhu",
        )
        record = store.get(replaced["data_version_id"])
        self.assertEqual(record["inputs"][0]["data_version_id"], published["data_version_id"])
        self.assertEqual(record["description"], "加工描述尚未生成")
        self.assertEqual(record["code_id"], "renew_beizhu")
        before = self.conn.execute(
            "SELECT data_version_id FROM doc_info WHERE doc_id = %s AND status = 'ready'",
            (published["doc_id"],),
        ).fetchone()

        class Boom:
            def upsert(self, record: dict) -> None:
                raise RuntimeError("mongo down")

        with self.assertRaises(Exception):
            replace_rows(
                self.conn,
                RecordingIndex(),
                published["doc_id"],
                [{"c1": "南", "c2": 1}],
                lineage=Boom(),
                code_id="renew_beizhu",
            )
        after = self.conn.execute(
            "SELECT data_version_id FROM doc_info WHERE doc_id = %s AND status = 'ready'",
            (published["doc_id"],),
        ).fetchone()
        self.assertEqual(before["data_version_id"], after["data_version_id"])

    def test_job_keeps_the_file_it_read_and_the_model_note(self) -> None:
        import tempfile
        from pathlib import Path

        import pandas as pd

        from structured.lineage import MemoryLineage

        filename = f"{self.prefix}_读文件.csv"
        payload = _csv(["区域", "金额"], [[self.prefix + "唯一", "10"]])
        published = self._publish(
            self._stage(filename, ["区域", "金额"], [[self.prefix + "唯一", "10"]]),
            {"金额": "度量", "区域": "维度"},
        )
        folder = Path(tempfile.mkdtemp())
        same_name = folder / filename
        same_name.write_text("区域,金额\n南,1\n", encoding="utf-8")
        same_bytes = folder / "另一路径.csv"
        same_bytes.write_bytes(payload)
        store = MemoryLineage()

        def renew(context) -> None:
            pd.read_csv(same_name)
            pd.read_csv(same_bytes)
            context.replace_rows([{"c1": "西", "c2": 20}])

        job = save_job(
            self.conn,
            name=f"{self.prefix} 读文件",
            sop_ref="SOP-读文件",
            run_at="09:30",
            doc_ids=[published["doc_id"]],
            code_id="read_file_demo",
        )
        done = run_job(
            self.conn,
            job["job_id"],
            {"read_file_demo": {"fn": renew, "doc_id": published["doc_id"]}},
            RecordingIndex(),
            today=datetime(2026, 11, 8).date(),
            narrator=lambda _source, files, _output, _code: {
                "description": "读取外部输入后写回",
                "steps": [{"order": 1, "action": f"读取{files[0]}并写回"}],
            },
            lineage=store,
        )
        record = next(iter(store.docs.values()))
        self.assertEqual(done["last_status"], "succeeded")
        self.assertEqual([item["source_file"] for item in record["unresolved"]], [filename])
        self.assertEqual(record["inputs"][0]["doc_id"], published["doc_id"])
        self.assertEqual(record["inputs"][0]["source_file"], filename)
        self.assertEqual(record["description"], "读取外部输入后写回")
        self.assertIn(filename, record["steps"][0]["action"])

    def test_read_only_question_does_not_write_back(self) -> None:
        from structured.lineage import MemoryLineage, lookup_lineage
        from structured.query_table import statement_timeout_ms
        from structured.writeback import write_back_rows

        self.assertEqual(statement_timeout_ms(None), 30 * 60 * 1000)
        self.assertEqual(statement_timeout_ms(10), 10)
        self.assertEqual(statement_timeout_ms(10**12), 30 * 60 * 1000)
        filename = f"{self.prefix}_写回.csv"
        published = self._publish(self._stage(filename, ["区域", "金额"], [["东", "10"]]), {"金额": "度量", "区域": "维度"})
        found = query_table_data(
            "区域",
            conn=self.conn,
            plan={"table_id": published["table_id"], "columns": ["c1", "c2"]},
        )
        table = found["table_results"][0]
        skipped = write_back_rows(
            self.conn,
            RecordingIndex(),
            table["table_id"],
            table["rows"],
            question="有几行",
            lineage=MemoryLineage(),
        )
        self.assertFalse(skipped["written"])
        self.assertEqual(skipped["message"], "只读提问不写回")
        store = MemoryLineage()
        saved = write_back_rows(
            self.conn,
            RecordingIndex(),
            table["table_id"],
            table["rows"],
            question="把这次结果写回这张表",
            lineage=store,
        )
        self.assertTrue(saved["written"])
        looked = lookup_lineage(saved["data_version_id"], store)
        self.assertTrue(looked["found"])
        self.assertEqual(looked["record"]["producer"], "llm")
        self.assertIn("模型步骤", looked["record"]["steps"][0]["action"])
        self.assertEqual(lookup_lineage("dv_missing", store)["message"], "没有血缘")

        class Boom:
            def get(self, _lineage_id: str) -> dict:
                raise RuntimeError("down")

        self.assertEqual(lookup_lineage(saved["data_version_id"], Boom())["message"], "血缘读取失败")
        partial = write_back_rows(
            self.conn,
            RecordingIndex(),
            table["table_id"],
            [{"c1": "东", "未知": 1}],
            question="再写回这张表",
            lineage=MemoryLineage(),
        )
        self.assertFalse(partial["written"])

    def test_job_over_thirty_minutes_does_not_publish(self) -> None:
        from datetime import timedelta

        filename = f"{self.prefix}_超时.csv"
        published = self._publish(self._stage(filename, ["区域"], [["东"]]), {"区域": "维度"})
        start = datetime(2026, 11, 9, 1, 0, tzinfo=timezone.utc)
        times = {"now": start}

        def clock() -> datetime:
            return times["now"]

        def slow(context) -> None:
            times["now"] = start + timedelta(minutes=31)
            context.replace_rows([{"c1": "西"}])

        job = save_job(
            self.conn,
            name=f"{self.prefix} 超时",
            sop_ref="SOP-超时",
            run_at="09:00",
            doc_ids=[published["doc_id"]],
            code_id="slow_beizhu",
        )
        done = run_job(
            self.conn,
            job["job_id"],
            {"slow_beizhu": {"fn": slow, "doc_id": published["doc_id"]}},
            RecordingIndex(),
            today=start.date(),
            clock=clock,
        )
        self.assertEqual(done["last_status"], "failed")
        self.assertGreaterEqual(done["last_duration_seconds"], 30 * 60)
        current = self.conn.execute(
            "SELECT data_version_id FROM doc_info WHERE doc_id = %s AND status = 'ready'",
            (published["doc_id"],),
        ).fetchone()
        self.assertEqual(current["data_version_id"], published["data_version_id"])

    def test_due_clock_uses_beijing_time(self) -> None:
        from structured.jobs import _is_due

        job = {"run_at": "09:30", "schedule_kind": "daily", "schedule_day": None, "enabled": True}
        beijing_morning = datetime(2026, 11, 9, 1, 30, tzinfo=timezone.utc)
        self.assertTrue(_is_due(job, beijing_morning))
        utc_morning = datetime(2026, 11, 9, 9, 30, tzinfo=timezone.utc)
        self.assertFalse(_is_due(job, utc_morning))

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


class SkillMatchTests(TestCase):
    def test_one_high_score_applies_and_close_scores_wait(self):
        from structured.skills import _by_score

        skills = [
            {"name": "甲", "summary": "追加", "skill_id": "1", "instructions": "步骤甲"},
            {"name": "乙", "summary": "核对", "skill_id": "2", "instructions": "步骤乙"},
        ]
        chosen = _by_score("问题", skills, lambda _question, skill: 0.9 if skill["name"] == "甲" else 0.2)
        self.assertEqual(chosen["skill_id"], "1")
        self.assertEqual(_by_score("问题", skills, lambda _question, _skill: 0.95), "choose")
        self.assertEqual(
            _by_score("问题", skills, lambda _question, skill: 0.7 if skill["name"] == "甲" else 0.1),
            "choose",
        )
        self.assertIsNone(_by_score("问题", skills, lambda _question, _skill: 0.4))

    def test_lineage_list_returns_the_saved_record(self):
        from structured.lineage import MemoryLineage, build_lineage, list_lineage

        store = MemoryLineage()
        record = build_lineage(
            output={"doc_id": "doc", "table_id": "tbl", "import_version_id": "im", "data_version_id": "dv"},
            inputs=[{"doc_id": "doc", "import_version_id": "im", "data_version_id": "old"}],
        )
        store.upsert(record)
        listed = list_lineage(store)
        self.assertEqual(listed["records"][0]["lineage_id"], "dv")
        self.assertEqual(listed["message"], "")

    def test_lineage_page_uses_names_instead_of_ids(self):
        from structured.lineage import build_lineage, present_lineage

        record = build_lineage(
            output={"doc_id": "doc_1", "table_id": "tbl", "import_version_id": "iv", "data_version_id": "dv_new"},
            inputs=[{"doc_id": "doc_1", "import_version_id": "iv", "data_version_id": "dv_old", "source_file": "业绩表.csv"}],
            code_id="code_1",
            job_id="job_1",
            steps=[{"order": 1, "action": "完成分类后写回"}],
        )
        shown = present_lineage(
            [record],
            files={"doc_1": "业绩表.csv"},
            jobs={"job_1": "每日更新"},
            codes={"code_1": "每日更新"},
        )[0]
        self.assertEqual(shown["title"], "业绩表.csv")
        self.assertIn("每日更新", shown["subtitle"])
        self.assertNotIn("doc_1", shown["subtitle"])
        self.assertEqual(shown["inputs"], ["业绩表.csv"])
        self.assertEqual(shown["code_name"], "每日更新")
        self.assertNotIn("rows", shown)
        missing = present_lineage([record])[0]
        self.assertEqual(missing["title"], "文件已不在")
        self.assertNotIn("doc_1", missing["title"])

    def test_narration_parser_keeps_only_description_and_steps(self):
        from structured.lineage import parse_narration

        parsed = parse_narration(
            "```json\n{\"description\":\"按基准和地域分类\",\"steps\":[{\"order\":1,\"action\":\"读取基金、基准和地域\"}]}\n```"
        )
        self.assertEqual(parsed["description"], "按基准和地域分类")
        self.assertEqual(parsed["steps"][0]["action"], "读取基金、基准和地域")
        self.assertEqual(parse_narration("不是 JSON"), {})

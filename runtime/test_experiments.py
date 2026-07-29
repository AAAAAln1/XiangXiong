import argparse
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from runtime import benchmark_api, benchmark_vpn, execute, experiment, http_request
from scripts import memory_guard, session_start, stop_check


class RuntimeExperimentTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.results_dir = os.path.join(self.temp_dir.name, "results")
        self.patches = [
            mock.patch.object(experiment, "RESULTS_DIR", self.results_dir),
            mock.patch.object(execute, "RESULTS_DIR", self.results_dir),
            mock.patch.object(http_request, "RESULTS_DIR", self.results_dir),
            mock.patch.object(benchmark_api, "RESULTS_DIR", self.results_dir),
            mock.patch.object(session_start, "RESULTS_DIR", self.results_dir),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        self.temp_dir.cleanup()

    def create_closed_experiment(self, target, action, observation, conclusion, status):
        created = experiment.create_experiment(target, action=action)
        experiment.append_observation(target, created["experiment_id"], observation)
        experiment.close_experiment(target, created["experiment_id"], conclusion, status)
        return created["experiment_id"]

    def test_session_start_cognition_is_fixed_bounded_and_isolated(self):
        root = os.path.join(self.temp_dir.name, "project")
        cognition_dir = os.path.join(root, "cognition")
        knowledge_dir = os.path.join(root, "knowledge")
        os.makedirs(cognition_dir)
        os.makedirs(knowledge_dir)
        for index, name in enumerate(session_start.COGNITION_MODULES):
            with open(os.path.join(cognition_dir, name), "w", encoding="utf-8") as file:
                file.write(str(index) * (session_start.MAX_COGNITION_FILE_CHARS + 20))
        with open(os.path.join(knowledge_dir, "hidden.md"), "w", encoding="utf-8") as file:
            file.write("KNOWLEDGE_MUST_NOT_LOAD")

        modules = session_start.load_cognition_modules(root)
        rendered = session_start.render_cognition(modules)

        self.assertEqual(list(session_start.COGNITION_MODULES), [name for name, _ in modules])
        self.assertIn("[COGNITION]", rendered)
        self.assertNotIn("KNOWLEDGE_MUST_NOT_LOAD", rendered)
        self.assertTrue(all(len(content) <= session_start.MAX_COGNITION_FILE_CHARS
                            for _, content in modules))
        self.assertLessEqual(sum(len(content) for _, content in modules),
                             session_start.MAX_COGNITION_TOTAL_CHARS)

    def test_session_start_cognition_missing_module_does_not_block(self):
        root = os.path.join(self.temp_dir.name, "missing-project")
        cognition_dir = os.path.join(root, "cognition")
        os.makedirs(cognition_dir)
        present = session_start.COGNITION_MODULES[0]
        with open(os.path.join(cognition_dir, present), "w", encoding="utf-8") as file:
            file.write("present content")

        rendered = session_start.render_cognition(session_start.load_cognition_modules(root))

        self.assertIn("present content", rendered)
        self.assertIn(f"missing: cognition/{session_start.COGNITION_MODULES[1]}", rendered)
        self.assertIn(f"--- cognition/{session_start.COGNITION_MODULES[-1]} ---", rendered)

    def test_anti_compression_is_fixed_short_exposure_without_fc_body(self):
        """Exposure lens: fixed 6 lines (4 anti-compress + 2 support-constrain); no FC body."""
        rendered = session_start.render_anti_compression()
        self.assertIn("[ANTI-COMPRESSION]", rendered)
        self.assertEqual(6, len(session_start.ANTI_COMPRESSION_LINES))
        for line in session_start.ANTI_COMPRESSION_LINES:
            self.assertIn(line, rendered)
        # First 4: unjustified world compression guards.
        for line in session_start.ANTI_COMPRESSION_LINES[:4]:
            self.assertTrue("≠" in line or "!=" in line, line)
        # Last 2: supported explanation constrains next hop; unknowns not equal-priority.
        self.assertIn("Supported explanations should constrain the next experiment.", rendered)
        self.assertIn(
            "Preserved unknowns do not retain equal priority outside the current explanation boundary.",
            rendered,
        )
        self.assertNotIn("false_closures", rendered)
        self.assertNotIn("therefore", rendered.lower())
        self.assertNotIn("fuzz", rendered.lower())
        self.assertNotIn("check logout", rendered.lower())
        self.assertNotIn("False Openness", rendered)
        self.assertNotIn("1.", rendered)
        self.assertNotIn("2.", rendered)

    def test_recent_projection_order_and_target_isolation(self):
        target = "host:443/api\\v1"
        expected = [
            self.create_closed_experiment(target, "SQLi", "SQLi failed", "not observed", "failed"),
            self.create_closed_experiment(target, "upload", "upload failed", "not observed", "failed"),
            self.create_closed_experiment(target, "admin", "admin success", "observed", "success"),
        ]
        self.create_closed_experiment("other", "other action", "other observation", "other", "success")

        recent = session_start.load_recent_experiments(target)
        self.assertEqual(expected, [item["experiment_id"] for item in recent])
        output = io.StringIO()
        with redirect_stdout(output):
            session_start.print_recent_experiments(target)
        projection = output.getvalue()
        self.assertIn("[RECENT EXPERIMENTS]", projection)
        self.assertLess(projection.index("SQLi failed"), projection.index("upload failed"))
        self.assertLess(projection.index("upload failed"), projection.index("admin success"))
        self.assertIn("evidence: experiments/exp-001/evidence.md", projection)
        self.assertNotIn("other observation", projection)

    def test_world_model_projection_classifies_current_reasoning_state(self):
        lines = [
            "target: example.test\n",
            "surface: /admin\n",
            "tech: nginx\n",
            "artifact: responses/req_001.json\n",
            "hypothesis: admin endpoint requires authentication\n",
            "counter: default credentials rejected\n",
            "dead: SQL injection exhausted [0.0]\n",
            "dead-summary: legacy paths exhausted\n",
            "future: inspect a separate strategic direction\n",
        ]

        model = session_start.extract_world_model(lines)

        self.assertEqual("example.test", model["target"])
        self.assertEqual(["surface: /admin"], model["objects"])
        self.assertEqual(
            ["tech: nginx", "artifact: responses/req_001.json"],
            model["constraints"],
        )
        self.assertEqual(
            ["hypothesis: admin endpoint requires authentication"],
            model["unknowns"],
        )
        self.assertEqual(
            [
                "counter: default credentials rejected",
                "dead: SQL injection exhausted [0.0]",
                "dead-summary: legacy paths exhausted",
            ],
            model["counters"],
        )
        self.assertNotIn("future", json.dumps(model))

    def test_counter_only_state_resumes_regardless_of_report(self):
        """report.md is artifact metadata only; must not gate resume projection."""
        for state_line in ("counter: access denied", "dead-summary: legacy paths exhausted"):
            with self.subTest(state_line=state_line):
                model = session_start.extract_world_model([
                    "target: example.test",
                    state_line,
                ])
                self.assertTrue(session_start.should_resume(model))

    def test_report_present_does_not_block_resume_with_unknowns(self):
        model = session_start.extract_world_model([
            "target: example.test",
            "vuln: reset bypass confirmed",
            "hypothesis: privilege boundary untested",
            "artifact: results/example.test/report.md",
        ])
        self.assertTrue(session_start.should_resume(model))
        self.assertTrue(model["unknowns"])
        self.assertTrue(model["objects"])

    def test_resume_requires_target_and_state(self):
        self.assertFalse(session_start.should_resume(session_start.extract_world_model([])))
        self.assertFalse(
            session_start.should_resume(
                session_start.extract_world_model(["target: example.test"])
            )
        )
        self.assertTrue(
            session_start.should_resume(
                session_start.extract_world_model(
                    ["target: example.test", "surface: /login 200"]
                )
            )
        )

    def test_recent_projection_shows_active_hypothesis_and_last_artifact(self):
        target = "active-projection"
        created = experiment.create_experiment(
            target, hypothesis="gateway routes to auth service", activate=True
        )
        experiment.append_observation(
            target,
            created["experiment_id"],
            "gateway response differs by host",
            "responses/req_007.json",
        )

        output = io.StringIO()
        with redirect_stdout(output):
            session_start.print_recent_experiments(target)
        projection = output.getvalue()

        self.assertIn("active: true", projection)
        self.assertIn("hypothesis: gateway routes to auth service", projection)
        self.assertIn("artifact: responses/req_007.json", projection)
        self.assertNotIn("evidence:", projection)

    def test_world_model_preserves_each_single_line_vulnerability(self):
        model = session_start.extract_world_model([
            "target: example.test\n",
            "vuln: first finding | evidence: exp-001/req_001.json\n",
            "vuln: second finding | evidence: exp-002/req_004.json\n",
        ])

        self.assertEqual(2, len(model["objects"]))
        self.assertIn("first finding", model["objects"][0])
        self.assertIn("second finding", model["objects"][1])

    def test_recent_projection_limits_five_items_and_truncates_each_text_field(self):
        target = "bounded-context"
        experiment_ids = []
        for index in range(7):
            marker = str(index)
            experiment_ids.append(self.create_closed_experiment(
                target,
                marker + "A" * 300,
                marker + "O" * 300,
                marker + "C" * 300,
                "failed",
            ))

        recent = session_start.load_recent_experiments(target)
        self.assertEqual(experiment_ids[-5:], [item["experiment_id"] for item in recent])

        output = io.StringIO()
        with redirect_stdout(output):
            session_start.print_recent_experiments(target)
        projected_items = [line for line in output.getvalue().splitlines() if line.startswith("  id:")]
        self.assertEqual(5, len(projected_items))
        for index, line in enumerate(projected_items, start=2):
            marker = str(index)
            self.assertIn(marker + "A" * 239 + "...", line)
            self.assertIn(marker + "O" * 239 + "...", line)
            self.assertIn(marker + "C" * 239 + "...", line)
            self.assertNotIn(marker + "A" * 240, line)
            self.assertNotIn(marker + "O" * 240, line)
            self.assertNotIn(marker + "C" * 240, line)

    def test_execute_persists_full_output_and_appends_observation(self):
        target = "execute-target"
        experiment.create_experiment(target, action="run", experiment_id="exp-001")
        args = argparse.Namespace(
            experiment_id="exp-001",
            command="dummy-command",
            target=target,
            name="experiment",
            purpose=None,
            timeout=5,
        )
        execution = {
            "status": "success",
            "exit_code": 0,
            "stdout": "x" * (execute.MAX_OUTPUT_CHARS + 20),
            "stderr": "full stderr",
            "duration": 0.25,
            "reason": None,
        }
        with mock.patch.object(execute, "run_command", return_value=execution):
            result = execute.build_result(args)

        metadata_path = os.path.join(self.results_dir, experiment.safe_target(target), *result["metadata"].split("/"))
        with open(metadata_path, "r", encoding="utf-8") as file:
            metadata = json.load(file)
        stdout_path = os.path.join(self.results_dir, experiment.safe_target(target), *metadata["artifacts"]["stdout"].split("/"))
        with open(stdout_path, "r", encoding="utf-8") as file:
            self.assertEqual(execution["stdout"], file.read())
        linked = experiment.load_experiment(target, "exp-001")["observations"][-1]
        self.assertIn("status: success", linked["observation"])
        self.assertEqual(result["metadata"], linked["artifact"])
        self.assertIn("truncated", result["stdout"])

    def test_http_metadata_redaction_and_experiment_link(self):
        target = "http-target"
        experiment.create_experiment(target, action="request", experiment_id="exp-001")

        def fake_run(command, **kwargs):
            body_path = command[command.index("-o") + 1]
            with open(body_path, "w", encoding="utf-8") as file:
                file.write("response body")
            return subprocess.CompletedProcess(command, 0, "201|13|0.125", "curl warning")

        curl_args = [
            "-X", "POST", "https://example.test/admin", "-H", "Authorization: Bearer secret",
            "-H", "Cookie: sid=secret", "-H", "X-Test: visible",
        ]
        with mock.patch.object(http_request.subprocess, "run", side_effect=fake_run):
            result = http_request.run_curl(target, curl_args, "exp-001")

        metadata_path = os.path.join(
            self.results_dir,
            experiment.safe_target(target),
            "responses",
            result["metadata_filename"],
        )
        with open(metadata_path, "r", encoding="utf-8") as file:
            metadata = json.load(file)
        serialized = json.dumps(metadata)
        self.assertEqual("POST", metadata["method"])
        self.assertEqual("https://example.test/admin", metadata["url"])
        self.assertNotIn("Bearer secret", serialized)
        self.assertNotIn("sid=secret", serialized)
        self.assertIn("X-Test: visible", serialized)
        linked = experiment.load_experiment(target, "exp-001")["observations"][-1]
        self.assertIn("status: 201", linked["observation"])
        self.assertEqual("responses/req_001.json", linked["artifact"])

    def test_http_method_option_is_translated_before_curl_execution(self):
        target = "method-target"
        captured = {}

        def fake_run(command, **kwargs):
            captured["command"] = command
            body_path = command[command.index("-o") + 1]
            with open(body_path, "w", encoding="utf-8") as file:
                file.write("reachable")
            return subprocess.CompletedProcess(command, 0, "200|9|0.05", "")

        args = ["--method", "GET", "--url", "http://121.41.102.225:8000/"]
        with mock.patch.object(http_request.subprocess, "run", side_effect=fake_run):
            result = http_request.run_curl(target, args)

        self.assertNotIn("--method", captured["command"])
        self.assertIn("-X", captured["command"])
        self.assertEqual("GET", captured["command"][captured["command"].index("-X") + 1])
        self.assertEqual("200", result["status_code"])
        self.assertEqual("reachable", result["body"])

    def test_http_method_option_requires_value(self):
        with self.assertRaisesRegex(ValueError, "--method 缺少值"):
            http_request.normalize_curl_args(["--method"])

    def test_http_curl_timeout_is_reported_as_execution_error(self):
        target = "timeout-target"

        def fake_run(command, **kwargs):
            return subprocess.CompletedProcess(command, 28, "000|0|15.0", "")

        with mock.patch.object(http_request.subprocess, "run", side_effect=fake_run):
            result = http_request.run_curl(target, ["https://example.test"])

        self.assertEqual("000", result["status_code"])
        self.assertEqual(28, result["return_code"])
        self.assertIn("请求超时", result["error"])
        output = http_request.format_output(result)
        self.assertIn("Status: ERROR", output)
        self.assertIn("退出码 28", output)

        metadata_path = os.path.join(
            self.results_dir, experiment.safe_target(target), "responses", result["metadata_filename"]
        )
        with open(metadata_path, "r", encoding="utf-8") as file:
            metadata = json.load(file)
        self.assertEqual("000", metadata["status"])
        self.assertEqual(28, metadata["return_code"])

    def test_active_pointer_lifecycle_isolation_and_close_behavior(self):
        target = "active-target"
        first = experiment.create_experiment(
            target, hypothesis="active hypothesis", experiment_id="exp-001", activate=True
        )
        second = experiment.create_experiment(target, experiment_id="exp-002")
        self.assertEqual(first["experiment_id"], experiment.resolve_experiment_id(target))
        self.assertIsNone(experiment.resolve_experiment_id("other-target"))

        experiment.close_experiment(target, second["experiment_id"], status="failed")
        self.assertEqual(first["experiment_id"], experiment.resolve_experiment_id(target))
        result = experiment.close_experiment(target, status="success")
        self.assertEqual(first["experiment_id"], result["experiment_id"])
        self.assertEqual("experiments/exp-001/evidence.md", result["evidence"])
        self.assertEqual(0, result["artifact_count"])
        self.assertIsNone(experiment.resolve_experiment_id(target))
        self.assertFalse(os.path.exists(experiment.active_path(target)))

    def test_close_evidence_lists_linked_artifacts_once(self):
        target = "evidence-target"
        experiment.create_experiment(
            target, hypothesis="evidence hypothesis", experiment_id="exp-001", activate=True
        )
        experiment.append_observation(
            target, "exp-001", "first", "responses/req_001.json"
        )
        experiment.append_observation(
            target, "exp-001", "second", "responses/req_001.json"
        )

        result = experiment.close_experiment(
            target, conclusion="confirmed", status="success"
        )

        evidence_path = os.path.join(
            self.results_dir, target, "experiments", "exp-001", "evidence.md"
        )
        with open(evidence_path, "r", encoding="utf-8") as file:
            evidence = file.read()
        self.assertEqual(1, evidence.count("responses/req_001.json"))
        self.assertIn("confirmed", evidence)
        self.assertEqual(1, result["artifact_count"])

    def test_active_experiment_requires_hypothesis_and_cannot_be_replaced(self):
        target = "active-contract"
        with self.assertRaisesRegex(ValueError, "必须提供 hypothesis"):
            experiment.create_experiment(target, activate=True)

        first = experiment.create_experiment(
            target, hypothesis="first hypothesis", activate=True
        )
        second = experiment.create_experiment(
            target, hypothesis="second hypothesis"
        )
        with self.assertRaisesRegex(ValueError, first["experiment_id"]):
            experiment.activate_experiment(target, second["experiment_id"])
        with self.assertRaisesRegex(ValueError, first["experiment_id"]):
            experiment.create_experiment(
                target, hypothesis="third hypothesis", activate=True
            )

    def test_http_active_auto_link_explicit_priority_and_no_active_behavior(self):
        target = "http-active"
        experiment.create_experiment(
            target, hypothesis="http hypothesis", experiment_id="exp-001", activate=True
        )
        experiment.create_experiment(target, experiment_id="exp-002")

        def fake_run(command, **kwargs):
            body_path = command[command.index("-o") + 1]
            with open(body_path, "w", encoding="utf-8") as file:
                file.write("ok")
            return subprocess.CompletedProcess(command, 0, "200|2|0.01", "")

        with mock.patch.object(http_request.subprocess, "run", side_effect=fake_run):
            automatic = http_request.run_curl(target, ["https://example.test"])
            explicit = http_request.run_curl(target, ["https://example.test"], "exp-002")
            unlinked = http_request.run_curl("no-active", ["https://example.test"])
        self.assertIsNone(automatic["association_error"])
        self.assertEqual(1, len(experiment.load_experiment(target, "exp-001")["observations"]))
        self.assertEqual(1, len(experiment.load_experiment(target, "exp-002")["observations"]))
        self.assertIsNone(unlinked["association_error"])

    def test_stale_active_variants_do_not_block_http_or_create_experiment(self):
        def fake_run(command, **kwargs):
            body_path = command[command.index("-o") + 1]
            with open(body_path, "w", encoding="utf-8") as file:
                file.write("")
            return subprocess.CompletedProcess(command, 0, "404|0|0.01", "")

        for index, pointer in enumerate(("bad", "exp-999", "exp-001")):
            target = f"stale-{index}"
            os.makedirs(experiment.experiments_dir(target), exist_ok=True)
            if pointer == "exp-001":
                experiment.create_experiment(target, experiment_id=pointer)
                metadata_path = experiment.metadata_path(target, pointer)
                with open(metadata_path, "w", encoding="utf-8") as file:
                    file.write("{bad json")
            with open(experiment.active_path(target), "w", encoding="utf-8") as file:
                file.write(pointer)
            with mock.patch.object(http_request.subprocess, "run", side_effect=fake_run):
                result = http_request.run_curl(target, ["https://example.test"])
            self.assertIsNone(result["association_error"])
            self.assertFalse(os.path.exists(experiment.active_path(target)))

    def test_execute_active_link_and_session_active_priority(self):
        target = "execute-active"
        ids = [experiment.create_experiment(target, experiment_id=f"exp-{index:03d}")["experiment_id"]
               for index in range(1, 7)]
        experiment.activate_experiment(target, ids[0])
        args = argparse.Namespace(experiment_id=None, command="dummy-command", target=target,
                                  name="experiment", purpose=None, timeout=5)
        execution = {"status": "success", "exit_code": 0, "stdout": "", "stderr": "",
                     "duration": 0.1, "reason": None}
        with mock.patch.object(execute, "run_command", return_value=execution):
            result = execute.build_result(args)
        self.assertEqual(ids[0], result["experiment_id"])
        self.assertNotEqual(result["execution_id"], result["experiment_id"])
        recent = session_start.load_recent_experiments(target)
        self.assertEqual(5, len(recent))
        self.assertEqual(ids[0], recent[0]["experiment_id"])
        self.assertTrue(recent[0]["active"])

    def test_memory_guard_denies_only_hot_memory(self):
        hot_memory = os.path.join(memory_guard.PROJECT_ROOT, ".claude", "memory.md")
        # Write 工具直接指向热 memory → deny
        with mock.patch("sys.stdin", io.StringIO(json.dumps({
            "tool_name": "Write",
            "tool_input": {"file_path": hot_memory}
        }))):
            output = io.StringIO()
            with redirect_stdout(output):
                memory_guard.main()
        decision = json.loads(output.getvalue())
        self.assertEqual(
            "deny", decision["hookSpecificOutput"]["permissionDecision"]
        )

        # Write 工具指向其他文件 → 允许
        with mock.patch("sys.stdin", io.StringIO(json.dumps({
            "tool_name": "Write",
            "tool_input": {"file_path": os.path.join(self.temp_dir.name, "memory.md")}
        }))):
            output = io.StringIO()
            with redirect_stdout(output):
                memory_guard.main()
        self.assertEqual("", output.getvalue())

        # Bash 重定向到热 memory → deny
        with mock.patch("sys.stdin", io.StringIO(json.dumps({
            "tool_name": "Bash",
            "tool_input": {"command": "printf '%s\\n' 'target:x' > .claude/memory.md"}
        }))):
            output = io.StringIO()
            with redirect_stdout(output):
                memory_guard.main()
        decision = json.loads(output.getvalue())
        self.assertEqual(
            "deny", decision["hookSpecificOutput"]["permissionDecision"]
        )

        # Bash 通过 merge.py 写入 → 允许
        with mock.patch("sys.stdin", io.StringIO(json.dumps({
            "tool_name": "Bash",
            "tool_input": {"command": "echo 'DELTA:' | python runtime/merge.py"}
        }))):
            output = io.StringIO()
            with redirect_stdout(output):
                memory_guard.main()
        self.assertEqual("", output.getvalue())

    def test_stop_check_detects_continue_question_from_last_assistant_message(self):
        transcript_path = os.path.join(self.temp_dir.name, "transcript.jsonl")
        entries = [
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "测试已完成。需要我继续深入吗？"}
            ]}},
        ]
        with open(transcript_path, "w", encoding="utf-8") as file:
            for entry in entries:
                file.write(json.dumps(entry, ensure_ascii=False) + "\n")

        message = stop_check.load_last_assistant_message(transcript_path)
        self.assertTrue(stop_check.asks_user_to_continue(message))
        self.assertFalse(stop_check.asks_user_to_continue("测试已完成。"))

    def test_stop_check_blocks_declared_next_step_from_real_failure_pattern(self):
        message = """测试总结\n\n下一步方向\n- 寻找可绕过登录的方式\n- 深入探测 OSS bucket 权限\n"""
        reason = stop_check.stop_conflict_reason(message)
        self.assertIn("已声明下一步", reason)

        recap = "下一步验证 SSO 服务器直接登录能否绕过应用层权限。"
        self.assertIn("已声明下一步", stop_check.stop_conflict_reason(recap))
        self.assertEqual("", stop_check.stop_conflict_reason("测试完成，所有已知方向均已穷尽。"))

    def test_stop_check_blocks_unapplied_delta(self):
        message = """测试总结\n\nDELTA:\nsurface: new service\nhypothesis: auth relation unknown [0.5]\n"""
        reason = stop_check.stop_conflict_reason(message)
        self.assertIn("尚未应用的 DELTA", reason)

    def test_benchmark_start_journal_is_run_level_without_target_or_memory(self):
        config = {"token": "secret-token", "benchmark_id": "run:one/test\\id"}
        response = {"status": 200, "body": {"instance": "ready"}}
        with mock.patch.object(benchmark_api, "request", return_value=response) as mocked_request:
            result = benchmark_api.call_and_archive(
                config, "start", "POST", "/openapi/v1/challenges/start",
                query={"unique_code": "challenge-1"}, unique_code="challenge-1",
            )

        self.assertEqual(response, result)
        mocked_request.assert_called_once_with(
            config, "POST", "/openapi/v1/challenges/start",
            query={"unique_code": "challenge-1"}, body=None,
        )
        run_root = os.path.join(self.results_dir, "benchmarks", "run_one_test_id")
        journal_path = os.path.join(run_root, "logs", "api.jsonl")
        with open(journal_path, "r", encoding="utf-8") as file:
            journal = json.loads(file.readline())
        self.assertEqual("challenge-1", journal["unique_code"])
        self.assertEqual({"method": "POST", "path": "/openapi/v1/challenges/start"}, journal["request"])
        self.assertFalse(os.path.exists(os.path.join(run_root, "targets")))
        self.assertFalse(os.path.exists(os.path.join(run_root, "memory.md")))

    def test_benchmark_submit_flag_and_token_are_not_persisted(self):
        config = {"token": "secret-token", "benchmark_id": "submit-run"}
        flag = "flag{never-write-this}"
        response = {
            "status": 200,
            "body": {"message": f"accepted {flag}", "debug": "secret-token"},
        }
        with mock.patch.object(benchmark_api, "request", return_value=response):
            benchmark_api.call_and_archive(
                config, "submit", "POST", "/openapi/v1/challenges/submit",
                body={"unique_code": "challenge-2", "flag": flag},
                unique_code="challenge-2", flag=flag,
            )

        journal_path = os.path.join(
            self.results_dir, "benchmarks", "submit-run", "logs", "api.jsonl",
        )
        with open(journal_path, "r", encoding="utf-8") as file:
            persisted = file.read()
        self.assertNotIn(flag, persisted)
        self.assertNotIn(config["token"], persisted)
        self.assertNotIn('"flag"', persisted)

    def test_benchmark_list_writes_full_score_snapshot(self):
        config = {"token": "secret-token", "benchmark_id": "list-run"}
        response = {"status": 200, "body": {"score": 42, "challenges": [{"id": "a-01"}]}}
        with mock.patch.object(benchmark_api, "request", return_value=response):
            benchmark_api.call_and_archive(
                config, "list", "GET", "/openapi/v1/challenges",
            )

        run_root = os.path.join(self.results_dir, "benchmarks", "list-run")
        with open(os.path.join(run_root, "score.json"), "r", encoding="utf-8") as file:
            snapshot = json.load(file)
        self.assertEqual(response["status"], snapshot["status"])
        self.assertEqual(response["body"], snapshot["body"])
        self.assertIn("timestamp", snapshot)
        self.assertTrue(os.path.exists(os.path.join(run_root, "logs", "api.jsonl")))

    def test_vpn_prepare_openvpn_253_conversion_preserves_source(self):
        source_path = os.path.join(self.temp_dir.name, "下载 配置.ovpn")
        original = (
            "client\ncompat-mode 2.4.8\nignore-unknown-option compat-mode block-outside-dns\n"
            "data-ciphers AES-256-GCM\n<cert>\nCERTIFICATE BODY\n</cert>\n"
        )
        with open(source_path, "w", encoding="utf-8") as file:
            file.write(original)
        fake_executable = os.path.join(self.temp_dir.name, "OpenVPN Folder", "openvpn.exe")
        os.makedirs(os.path.dirname(fake_executable))
        with open(fake_executable, "wb") as file:
            file.write(b"")
        config = {"vpn_config": source_path, "benchmark_id": "vpn:test"}

        with mock.patch.object(benchmark_vpn, "RESULTS_DIR", self.results_dir), \
                mock.patch.object(benchmark_vpn, "find_openvpn", return_value=fake_executable), \
                mock.patch.object(benchmark_vpn, "read_openvpn_version", return_value=("2.5.3", (2, 5, 3))):
            result = benchmark_vpn.prepare(config)

        with open(source_path, "r", encoding="utf-8") as file:
            self.assertEqual(original, file.read())
        with open(result["prepared_path"], "r", encoding="utf-8") as file:
            prepared = file.read()
        self.assertNotIn("compat-mode 2.4.8", prepared)
        self.assertIn("ignore-unknown-option block-outside-dns", prepared)
        self.assertIn("data-ciphers AES-256-GCM:AES-128-CBC", prepared)
        self.assertIn("CERTIFICATE BODY", prepared)
        metadata_path = os.path.join(self.results_dir, "benchmarks", "vpn_test", "vpn", "metadata.json")
        with open(metadata_path, "r", encoding="utf-8") as file:
            metadata = json.load(file)
        self.assertEqual("2.5.3", metadata["openvpn_version"])
        self.assertNotIn("CERTIFICATE BODY", json.dumps(metadata))

    def test_vpn_config_priority(self):
        config_path = os.path.join(self.temp_dir.name, ".benchmark.json")
        with open(config_path, "w", encoding="utf-8") as file:
            json.dump({"vpn_config": "file.ovpn", "benchmark_id": "run"}, file)
        with mock.patch.object(benchmark_vpn, "CONFIG_PATH", config_path), \
                mock.patch.dict(os.environ, {"BENCHMARK_VPN_CONFIG": "env.ovpn"}, clear=False):
            self.assertTrue(benchmark_vpn.load_config()["vpn_config"].endswith("env.ovpn"))
            self.assertTrue(benchmark_vpn.load_config("cli.ovpn")["vpn_config"].endswith("cli.ovpn"))
        with mock.patch.object(benchmark_vpn, "CONFIG_PATH", config_path), \
                mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(os.path.join(self.temp_dir.name, "file.ovpn"),
                             benchmark_vpn.load_config()["vpn_config"])

    def test_vpn_ensure_existing_route_does_not_start(self):
        config = {"vpn_config": "unused.ovpn"}
        facts = {"route_present": True, "gateway_present": True, "network": "10.0.128.0/18",
                 "gateway": "10.254.0.1", "error": None}
        with mock.patch.object(benchmark_vpn, "status", return_value={"facts": facts}), \
                mock.patch.object(benchmark_vpn, "prepare") as mocked_prepare, \
                mock.patch.object(benchmark_vpn, "start_elevated") as mocked_start:
            result = benchmark_vpn.ensure(config)
        self.assertTrue(result["ok"])
        self.assertFalse(result["started"])
        mocked_prepare.assert_not_called()
        mocked_start.assert_not_called()

    def test_vpn_route_requires_configured_gateway(self):
        facts = {"route_present": True, "gateway_present": False}
        self.assertFalse(benchmark_vpn.route_ready(facts))
        self.assertTrue(benchmark_vpn.route_ready({"route_present": True, "gateway_present": None}))

    def test_vpn_elevated_start_passes_paths_through_environment(self):
        completed = subprocess.CompletedProcess([], 0, stdout="123", stderr="")
        with mock.patch.object(benchmark_vpn.subprocess, "run", return_value=completed) as mocked_run:
            result = benchmark_vpn.start_elevated(
                r"C:\Program Files\OpenVPN\bin\openvpn.exe",
                r"F:\含 空格\client.ovpn",
                r"F:\含 空格\vpn.log",
            )
        self.assertTrue(result["started"])
        call = mocked_run.call_args
        self.assertEqual("123", str(result["pid"]))
        self.assertEqual(r"C:\Program Files\OpenVPN\bin\openvpn.exe",
                         call.kwargs["env"]["XIANGXIONG_OPENVPN_EXE"])
        self.assertEqual(r"F:\含 空格\client.ovpn", call.kwargs["env"]["XIANGXIONG_VPN_CONFIG"])
        self.assertNotIn(r"F:\含 空格\client.ovpn", call.args[0])

    def test_vpn_ensure_starts_once_and_polls_until_route_exists(self):
        config = {"vpn_config": "source.ovpn", "benchmark_id": "run"}
        missing = {"route_present": False, "gateway_present": False, "network": "10.0.128.0/18",
                   "gateway": "10.254.0.1", "error": None}
        present = dict(missing, route_present=True, gateway_present=True)
        prepared = {"openvpn_executable": "openvpn.exe", "prepared_path": "client.ovpn"}
        with mock.patch.object(benchmark_vpn, "RESULTS_DIR", self.results_dir), \
                mock.patch.object(benchmark_vpn, "status", side_effect=[{"facts": missing}, {"facts": missing},
                                                                        {"facts": present}]) as mocked_status, \
                mock.patch.object(benchmark_vpn, "prepare", return_value=prepared), \
                mock.patch.object(benchmark_vpn, "same_config_running", return_value=False), \
                mock.patch.object(benchmark_vpn, "start_elevated",
                                  return_value={"started": True, "pid": 123}) as mocked_start, \
                mock.patch.object(benchmark_vpn.time, "sleep"):
            result = benchmark_vpn.ensure(config, wait_seconds=5, poll_interval=0)
        self.assertTrue(result["ok"])
        self.assertTrue(result["started"])
        mocked_start.assert_called_once()
        self.assertEqual(3, mocked_status.call_count)


if __name__ == "__main__":
    unittest.main()

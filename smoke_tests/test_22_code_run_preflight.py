import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import ga


ROOT = Path(__file__).resolve().parents[1]


def exhaust(generator):
    output = []
    while True:
        try:
            output.append(next(generator))
        except StopIteration as stop:
            return output, stop.value


class CodeRunPreflightTest(unittest.TestCase):
    def test_invalid_python_is_rejected_before_temp_file_execution(self):
        code = 'items = [\n    "7: 台灯"看到"萤火虫，灯罩微抬",\n]\n'
        with tempfile.TemporaryDirectory() as temp_dir:
            output, result = exhaust(
                ga.code_run(code, cwd=temp_dir, code_cwd=temp_dir)
            )

            self.assertEqual(result["status"], "error")
            self.assertEqual(result["error_type"], "SyntaxError")
            self.assertEqual(result["line"], 2)
            self.assertIn('台灯"看到"萤火虫', result["source"])
            self.assertIn("file_write", result["hint"])
            self.assertIn("Syntax preflight failed", "".join(output))
            self.assertEqual(list(Path(temp_dir).glob("*.ai.py")), [])

    def test_valid_python_still_executes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _, result = exhaust(
                ga.code_run(
                    'print(\'台灯"看到"萤火虫\')',
                    cwd=temp_dir,
                    code_cwd=temp_dir,
                )
            )

            self.assertEqual(result["status"], "success")
            self.assertEqual(result["exit_code"], 0)
            self.assertIn('台灯"看到"萤火虫', result["stdout"])

    def test_tool_schema_discourages_prose_heavy_code(self):
        schema = json.loads(
            (ROOT / "assets" / "tools_schema_film.json").read_text(encoding="utf-8")
        )
        code_tool = next(
            item["function"]
            for item in schema
            if item["function"]["name"] == "code_run"
        )

        self.assertIn("Do NOT use code_run to author JSON", code_tool["description"])
        self.assertIn("file_write", code_tool["description"])

    def test_invalid_json_does_not_overwrite_existing_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "director_plan.json"
            original = '{"status": "valid"}\n'
            path.write_text(original, encoding="utf-8")
            handler = ga.GenericAgentHandler(SimpleNamespace(), cwd=temp_dir)
            response = SimpleNamespace(content="")

            _, outcome = exhaust(
                handler.do_file_write(
                    {
                        "path": str(path),
                        "content": '{"action": "台灯"看到"萤火虫"}',
                    },
                    response,
                )
            )

            self.assertEqual(outcome.data["status"], "error")
            self.assertEqual(outcome.data["error_type"], "JSONDecodeError")
            self.assertEqual(path.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()

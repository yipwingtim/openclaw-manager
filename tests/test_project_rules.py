import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]


class ProjectRuleTests(unittest.TestCase):
    def test_agent_rules_require_tests_and_consistency_checks(self):
        rules = (ROOT_DIR / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("每条新增或修改的可执行规则都必须有对应的自动化测试", rules)
        self.assertIn("影响生产一致性的规则还必须同步更新校验脚本", rules)


if __name__ == "__main__":
    unittest.main()

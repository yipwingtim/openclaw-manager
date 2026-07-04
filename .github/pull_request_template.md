# Summary / 摘要

English:
- Briefly describe what this PR changes.
- Focus on the final behavior or outcome.

中文：
- 简要说明本 PR 修改了什么。
- 重点说明最终行为或结果变化。

---

# Reason / 背景

English:
- Explain why this change is needed.
- Describe the problem, limitation, security concern, or product requirement addressed by this PR.

中文：
- 说明为什么需要这个改动。
- 描述本 PR 解决的问题、限制、安全风险或产品需求。

---

# Changes / 变更

English:
- List the main code, configuration, documentation, or test changes.
- Keep each item concise and focused.

中文：
- 列出主要代码、配置、文档或测试变更。
- 每一项尽量简洁、聚焦。

---

# Tests / 测试

English:
- List the automated tests that were run.
- Include manual verification steps if applicable.
- If tests were not run, explain why.

中文：
- 列出已执行的自动化测试。
- 如有手工验证步骤，也请一并说明。
- 如未执行测试，请说明原因。

```bash
# Example / 示例：
python3 tests/test_manager_web_lifecycle.py
python3 -m unittest discover -s tests -p 'test_*.py'
```

---

# Risk / 风险

English:
- Risk level: Low / Medium / High.
- Explain possible side effects, behavior changes, or operational impact.

中文：
- 风险等级：低 / 中 / 高。
- 说明可能的副作用、行为变化或运维影响。

---

# Compatibility / 兼容性

English:
- Describe whether this PR changes existing behavior, APIs, data structures, permissions, or configuration.
- If there is no compatibility impact, write: No compatibility impact.

中文：
- 说明本 PR 是否改变现有行为、接口、数据结构、权限或配置。
- 如无兼容性影响，请写：无兼容性影响。

---

# Rollback Plan / 回滚方案

English:
- Describe how to roll back this change if something goes wrong.
- Example: Revert this PR.

中文：
- 说明如果出现问题，如何回滚本次变更。
- 示例：回滚本 PR。

---

# Checklist / 检查清单

English:
- [ ] The change is limited to the intended scope.
- [ ] Tests have been added or updated where necessary.
- [ ] Existing behavior has been preserved unless intentionally changed.
- [ ] Security, compatibility, and rollback impact have been considered.

中文：
- [ ] 本次变更控制在预期范围内。
- [ ] 已根据需要新增或更新测试。
- [ ] 除非有意调整，否则保持现有行为不变。
- [ ] 已考虑安全、兼容性和回滚影响。

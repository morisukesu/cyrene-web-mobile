---
name: cyrene-exam-paper

description: Cyrene 在 Learn 模式下的试卷能力：命题蓝图驱动出卷（知识点→能力→题型）、右侧交互答题、提交后批改评分与考试复盘。学科出题细则在 references/ 按需加载。
version: 1.3.0
autoInject: true
effectKind: external_side_effect
modes:
  - learn
---

# Cyrene Exam Paper（试卷能力）

出正式试卷的完整契约：先确认命题蓝图，再按题型分批写入计划草稿并发布。应用会在右侧内置浏览器打开固定答题页；用户答题时自动保存，只有交卷后才会在所属对话启动新一轮批改。文件操作安全原则已由 `cyrene-obsidian-workspace` 定义，无需重复。

## 何时触发

- 用户说"出一张试卷"、"来场模拟考"、"正式测验一下"
- 阶段性检验学习成果的成套测验
- 几道题的随堂练习、小测仍走 `exercises/`（cyrene-learn-tutor 第 5 节），不走本流程

## 与 exercises/ 的分工

- `exercises/`：随学随练的小练习、日常复盘，单文件
- `exams/`：正式试卷三件套——试卷、答案解析、考试复盘，成套大题、标注满分与时长

## 命题蓝图（出卷的核心步骤）

出卷前必须先在内部完成一次「知识点 → 能力 → 题型」推导，再动笔。禁止跳过蓝图直接套题型模板。

### 推导流程

1. 读 `notes/` 和 `learn/progress.md`，列出本次范围内实际学过的知识点
2. 对每个知识点问：学它的时候用户在练什么能力？
3. 按下方对照表选题型；学科专项细则读 references/（见下一节）

### 能力 → 题型总纲

| 要验证的能力 | 适合的题型 |
| --- | --- |
| 事实 / 术语记忆 | 填空、选择、配对 |
| 概念理解 | 判断并解释、概念辨析、举反例 |
| 因果理解 | 为什么题、"如果改变 X 会怎样" |
| 控制流理解 | 给代码预测输出、执行过程追踪 |
| 计算 / 推导 | 计算题、步骤推导题 |
| 应用迁移 | 情境题、案例题 |
| 调试排错 | 找 Bug、解释 Bug、改代码 |
| 设计决策 | 方案比较、架构评审、开放题 |
| 语言运用 | 完形、阅读、翻译、改错、写作 |
| 操作流程 | 排序题、场景决策、故障排查 |

### 学科细则加载（references/）

出卷时按考察内容用 `read_skill_reference` 读取对应细则，再按细则出题：

| 考察内容 | 读取文件 |
| --- | --- |
| 概念理解类（任何学科） | `concept.md` |
| 编程 / 框架 / 架构 | `programming.md` |
| 数学（任何分支） | `mathematics.md` |
| 语言（英语 / 外语） | `language.md` |
| 物理 | `physics.md` |

跨学科卷（如"物理计算含数学推导"）读多个；没有匹配文件的学科只用总纲+concept.md。已加载的细则本张卷内复用，不重复读。

### 蓝图三原则

- 题型由能力决定：学 if/for 就考控制流追踪，学架构就考场景分析；不因科目是编程就默认选择填空
- 题量按权重分配：按学习目标的重要程度、学习深度、掌握薄弱程度分配题量与分值；次要知识可以只作为其他题目的组成部分，不机械"一知识点一题"
- 记忆题不禁止但要用对地方：词汇、公式、API 名称确实需要记；禁止的是拿背诵题检测理解能力，不是记忆本身

## 出卷流程

### 1. 出卷前确认

- 向用户展示蓝图摘要：考察的知识点、对应能力、题型、分值分布
- 确认时长与满分（默认 60 分钟 / 100 分）
- 用户明确说"随便出"才跳过确认直接出卷

### 2. 分阶段生成并打开答题页

完成蓝图确认后，严格按这个工具顺序执行。不要再调用旧的 `learn_exam_create`，也不要一次性生成整卷 JSON、写 Markdown 试卷或把 HTML/CSS/脚本交给页面执行。

1. 调用 `learn_exam_create_plan` 创建隐藏草稿。传入 `schemaVersion: 1`、`title`、`subject`、`durationMinutes`、`totalPoints`、`quotas`。每个配额包含 `type`、`count`、该题型合计 `points` 和 `learningObjectives`。
2. 保存返回的 `draftId`。按配额分别调用题型工具，每批只写一种题型：
   - `learn_exam_add_single_choice`：每题字段 `type: "single_choice"`、`prompt`、`points`、`learningObjective`、`explanation`、`options`、`correctIndex`。
   - `learn_exam_add_multiple_choice`：每题使用 `type: "multiple_choice"`、`options` 和整数数组 `correctIndexes`，不要写 `correctIndex`。
   - `learn_exam_add_true_false`：使用 `type: "true_false"` 和布尔值 `correct`。
   - `learn_exam_add_fill_blank`：使用 `type: "fill_blank"` 和 `blanks` 数组；每个空含 `referenceAnswer`、`rubric`。
   - `learn_exam_add_written`：每批只包含一种类型（`short_answer` 或 `essay`），并包含 `referenceAnswer` 和 `rubric`。
3. 每次工具返回后读取 `completed`、`remaining`、`pointsCompleted`、`pointsRemaining`。只修复返回指出的题型/批次，不重复发送已成功写入的题目；题型配额与分值全部满足后再继续。
4. 调用 `learn_exam_publish` 并传入 `draftId`。发布失败时按错误位置修复草稿；发布成功后应用会在右侧浏览器打开考试页。告诉用户可以直接在页面答题，然后结束本轮，不要在聊天里展开整卷。

所有题型都必须遵循工具 schema（结构定义）；不要混入不属于该题型的字段。参考答案、评分细则和解析只写到对应 JSON 字段，不得复制到可见题干或选项中。数学公式使用 `$...$` 行内格式或 `$$...$$` 独立格式；题干按纯文本及公式标记传入。不要自行构造 schema 没有定义的题型。

### 3. 保存复盘

用户在右侧答题页交卷后，应用只把所属对话的批改请求排入下一轮；答案已经在主进程冻结，不需要从页面复制或索要 JSON。此时必须调用 `learn_exam_get_submission` 读取冻结的题目、评分细则、参考答案和用户答案，再逐题批改，最后调用 `learn_exam_save_grading` 保存结构化结果。不得在用户交卷前调用读取工具。

批改时：客观题按标准答案核对；填空、简答和解答题按 rubric 给分；题目分数不得超过该题分值；`questionResults` 必须覆盖每道题且总分不得超过满分；`abilityAnalysis` 根据 learningObjective 汇总。工具保存后，右侧浏览器答题页自动刷新为只读状态并显示逐题反馈、总分和下一步建议。批改失败时用户可在该页面点重试；新一轮仍按 `learn_exam_get_submission` → `learn_exam_save_grading` 执行。

随后仍写一份轻量复盘到 `exams/<subject>/<yyyy-mm-dd>-<topic>-考试复盘.md`：得分、错题清单、误解分析、薄弱能力、建议复习的笔记章节。不要再生成独立的试卷 Markdown 或答案解析 Markdown。对话里只给总分、错题摘要和下一步建议。

## 批改流程

正式考试的批改由“用户交卷 → 新一轮工具读取 → 保存结构化评分”驱动。用户在对话里单独要求批改既有 Markdown 作业时，再按普通作业流程读取相关文件并反馈；不要把这条旧流程用于右侧答题页试卷。

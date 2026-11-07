# 流动性表盘 + 到期日提醒 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 流动性标签页顶部加 0–100「流动性指数」扇形表盘（含构成指标、组合信号、历史对比、慢变量闸门），总览换小表盘并在底部加衍生品到期日提醒，页面去掉所有公式文字。

**Architecture:** 纯计算放两个新模块：`metrics_gauge.py`（分项 z / 得分 / 合成 / 历史 / 组合 / 结论 / 闸门）和 `expiry.py`（到期日推算）。`build.py` 取约 800 天数据，调用它们组装 payload 的 `liquidity_index` 与 `calendar`；`summary.py` 的流动性结论卡改由指数驱动；`template.html` 渲染。

**Tech Stack:** Python 3.12 + pandas + pytest；单文件 HTML + 原生 JS + 内联 SVG（不加新依赖）。

**Spec:** `docs/superpowers/specs/2026-11-07-liquidity-gauge-design.md`（每个任务的执行者都要先读相关小节；表盘外观以 `scratchpad/mockup/liquidity-gauge.html` 为准，路径：`/private/tmp/claude-501/-Users-qijuntong-Documents-chengjia-miniprogram/0b316a28-a078-4e31-9630-956110edfdb2/scratchpad/mockup/liquidity-gauge.html`）

## Global Constraints

- Repo `/Users/qijuntong/Documents/market-dashboard`，分支 `main`；Python `.venv/bin/python`；测试 `.venv/bin/python -m pytest -q`。
- **不调用真实 tushare / FRED / 网络**；只用 `DemoSource` / `DemoFred` 与测试替身。不 push，不派子代理。
- 提交信息中文，结尾空一行 + `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`。
- 降级：任何数据缺失只让局部为空，构建不失败、页面不抛错（沿用 `_fetch` / `_safe` 模式；前端一律 `guard()` 包住）。
- 数据进 DOM 只用 `textContent` / `el()`；SVG 用 `createElementNS`。
- 旧存档（无 `liquidity_index` / `calendar` 字段）必须能渲染。
- 页面上不出现计算方法：不显示 σ、z、标准差、权重、阈值公式；方法只写 README。
- 档位边界：0–25 极度紧张、25–45 偏紧、45–55 中性、55–75 偏松、75–100 极度宽松（左闭右开，100 归极度宽松）。

## Review Focus

1. 某计分项整段缺失（如 FRED 挂掉）→ 指数仍出、结论注明「外部缺 N 项」，历史按每天可用项计算 —— Task 1 `test_index_missing_items_note`、Task 3 `test_gauge_fred_down`。
2. 换锚 / 换月台阶不应让指数跳 —— Task 1 `test_breaks_suppress_step`。
3. 最新数据比 D 旧超过 7 天（外汇、FRED 滞后）→ 该项视为缺失而不是用旧值 —— Task 1 `test_stale_component_dropped`。
4. 历史不足 1 年（`ago.y1` 取不到）→ 该格显示「—」不报错 —— Task 1 `test_ago_partial`、Task 4 旧存档 / 空数据渲染检查。
5. 到期日落在节假日 / 美股休市 → 正确顺延 / 提前 —— Task 2 `test_cn_shift_holiday`、`test_us_holiday_moves_earlier`。

---

### Task 1: `metrics_gauge.py` 纯计算

**Files:**
- Create: `metrics_gauge.py`
- Modify: `config.py`（加常量）
- Test: `tests/test_metrics_gauge.py`

**Interfaces:**
- Produces（`config.py`）：
  - `POLICY_RATES` 改为 spec §4.4 的完整表并在最前补 `("20240722", 1.70, "7天逆回购")`。
  - `GAUGE_ZONES = [(0, 25, "极度紧张"), (25, 45, "偏紧"), (45, 55, "中性"), (55, 75, "偏松"), (75, 100, "极度宽松")]`
  - `GAUGE = dict(n=5, window=250, min_obs=120, floor_bp=2.0, floor_log=0.001, stale_days=7, z_cond=1.0, combo_need=2, lookback_days=800)`
  - `GAUGE_COMPONENTS`：有序 list of dict，键 `key, name, hint, group("domestic"|"external"|"ref"), kind("pct_level"|"bp_level"|"price"), dir(+1|-1|0), unit`：
    `dr001_spread`(domestic, bp_level, −1, "bp")、`r007_dr007`(domestic, bp_level, −1, "bp")、`bond_fut`(domestic, price, +1, "元", hint "价格涨 = 收益率降")、`us10y`(external, pct_level, −1, "%")、`usd`(external, price, −1, "点", hint "FXCM")、`cnh`(external, price, −1, "元/美元", hint "数值涨 = 人民币贬")、`hy_oas`(external, pct_level, −1, "%")、`sofr_iorb`(external, bp_level, −1, "bp", hint "美元回购压力")、`brent`(external, price, −1, "美元/桶")、`gold`(ref, price, 0, "元/克", hint "上金所 Au99.99")、`sc_oil`(ref, price, 0, "元/桶", hint "上海原油主力")。名称用 spec §3 的中文名。
  - `GAUGE_COMBOS`：`[{"key": "domestic_tight", "name": "国内资金收紧", "conds": [("dr001_spread", +1, "DR001−政策利率走阔"), ("r007_dr007", +1, "R007−DR007 走阔"), ("bond_fut", -1, "国债期货下跌")]}, {"key": "external_tight", "name": "外部收紧", "conds": [("us10y", +1, "美债收益率上行"), ("usd", +1, "美元走强"), ("cnh", +1, "人民币走弱")]}, {"key": "risk_off", "name": "避险", "conds": [("bond_fut", +1, "国债期货上涨"), ("gold", +1, "黄金上涨"), ("hy_oas", +1, "高收益债利差走阔")]}]`（条件 = `sign · z ≥ z_cond`）
- Produces（`metrics_gauge.py`，索引均为 `YYYYMMDD` 字符串、升序）：
  - `change_n(x: pd.Series, kind: str, n: int) -> pd.Series`：`pct_level` → `(x_t − x_{t−n})×100`（bp）；`bp_level` → `x_t − x_{t−n}`；`price` → `ln(x_t/x_{t−n})`。按自身观测位移。
  - `break_mask(chg: pd.Series, break_dates: list[str], n: int) -> pd.Series`：若某 break 日 `b` 满足「`b` 在 `chg` 索引里，且位置在 `(t−n, t]`」→ 该 `t` 置 NaN；`b` 不在索引里时按「第一个 ≥ b 的索引日」处理。
  - `z_series(chg: pd.Series, window: int, min_obs: int, floor: float) -> pd.Series`：`s_t` = `chg` 在 `t` 之前（shift 1）最近 `window` 个非 NaN 的样本标准差（ddof=1）；样本 < `min_obs` → NaN；`z = chg / max(s, floor)`。
  - `score(z: float | None, dir: int) -> float | None`：`clip(50 + dir·z·50/3, 0, 100)`；`dir == 0` 或 z 无效 → None。
  - `zone(v: float | None) -> str | None`。
  - `component_frame(x: pd.Series, spec: dict, breaks: list[str], cfg: dict) -> pd.DataFrame`：列 `value, chg, z, score`，索引为 `x` 的日期。
  - `index_on(day: str, frames: dict[str, pd.DataFrame], specs: list[dict], cfg: dict) -> dict`：每项取 `日期 ≤ day` 的最后一行；该行日期早于 day 超过 `stale_days` 自然日 → 缺失。返回 `{"value", "domestic", "external", "missing": {"domestic": int, "external": int}, "items": {key: {"date","value","chg","z","score"}}}`；合成规则按 spec §4.2（组内有效项 < 一半 → 该组 None；两组 → 各 50%；一组 → 该组；都无 → None）。`value` 为未取整的 float。
  - `index_history(days: list[str], frames, specs, cfg) -> pd.Series`（对每个 day 调 `index_on` 取 value，None 保留为 NaN）。
  - `ago(hist: pd.Series, day: str) -> dict`：`prev` = day 之前最后一个非 NaN；`w1/m1/y1` = 自然日 day−7 / −30 / −365 当日或之前最近一个非 NaN；取不到 → None；值取整。
  - `combos(zs: dict[str, float | None], combo_specs: list, cfg: dict) -> list[dict]`：每个 `{"key","name","met","of":3,"on": met ≥ combo_need,"conds":[{"name","ok"}]}`。
  - `verdict(value, combos_out, items, specs, missing) -> tuple[str, str]`：返回 `(text, reason)`，规则 spec §4.6；`text = f"{zone}。{reason}"`；value 为 None → `("数据不足", "数据不足")`。
  - `gate(m1_yoy: pd.Series, sf_yoy: pd.Series, fed_net: pd.Series) -> dict`：spec §4.7 三条；返回 `{"ok": bool, "notes": [str]}`；缺数据的检查视为通过。「连续 3 个月下降」= 最近 4 个月度值严格递减；「4 周收缩超过 3%」= 最后一个值相对 4 个观测前下降 > 3%（文案带一位小数百分比）。

- [ ] **Step 1: 写失败测试 `tests/test_metrics_gauge.py`**，至少包含：
  - `test_change_n_kinds`：`pct_level` 序列 [4.00, …, 4.18]（第 6 个比第 1 个高 0.18）→ 18.0；`bp_level` 差值；`price` 等于 `ln` 比值。
  - `test_z_floor_and_min_obs`：常数 chg（标准差 0）→ 用 floor；样本 119 个 → NaN、120 个 → 有值。
  - `test_score_clip_and_dir`：`score(3, -1) == 0`、`score(-6, -1) == 100`、`score(0.6, +1) == 60`、`score(1, 0) is None`。
  - `test_breaks_suppress_step`：DR001 恒为 1.30，政策利率 1.40 → 1.25（锚名称变化），spread 台阶 +15bp；`break_mask` 后跨台阶的 5 个日期为 NaN，`index_on` 在台阶前后两天的国内分相同（±0.5 以内）。
  - `test_stale_component_dropped`：某项最后日期比 day 早 8 天 → `items[key]` 缺失、`missing` 计数 +1。
  - `test_index_groups`：国内 3 项全有、外部只 2 项 → `external is None`、`value == domestic`；两组都只 1 项 → `value is None`。
  - `test_index_missing_items_note`：外部 6 项缺 2 → `verdict` 的 reason 以「（外部缺 2 项）」结尾。
  - `test_combos_threshold`：3 个 z 为 1.2/1.0/0.5（方向 +1）→ `met == 2, on is True`；只 1 个 → `on is False`。
  - `test_verdict_reason_priority`：外部收紧触发 → reason 以「外部在收紧：」开头并包含 3 个条件名；无触发且所有得分 40–60 → reason == "各项都在常态范围内"。
  - `test_zone_edges`：`zone(25) == "偏紧"`、`zone(24.99) == "极度紧张"`、`zone(100) == "极度宽松"`、`zone(None) is None`。
  - `test_ago_partial`：只有 20 天历史 → `y1 is None`，`prev`、`w1` 有值。
  - `test_gate_rules`：M1 [5.0, 4.8, 4.6, 4.4] → 不通过，note「背景：M1 同比连续 3 个月回落」；社融同理「背景：社融增速连续 3 个月放缓」；fed_net 下降 3.4% → note「背景：美元净流动性 4 周收缩 3.4%」；数据空 → ok True。
- [ ] **Step 2:** `.venv/bin/python -m pytest -q tests/test_metrics_gauge.py` → 全部 FAIL（模块不存在）。
- [ ] **Step 3:** 实现 `config.py` 常量与 `metrics_gauge.py` 各函数（向量化或逐日循环均可；`index_history` 一年约 250 天 × 11 项，循环可接受）。
- [ ] **Step 4:** `.venv/bin/python -m pytest -q` 全绿。
- [ ] **Step 5:** 提交 `feat: 流动性指数计算模块`。

---

### Task 2: `expiry.py` 衍生品到期日

**Files:**
- Create: `expiry.py`
- Test: `tests/test_expiry.py`

**Interfaces:**
- Produces：
  - `expiry.US_HOLIDAYS = ["20261126", "20261225", "20270101", "20270118", "20270215", "20270326", "20270531", "20270618", "20270705", "20270906", "20271125", "20271224"]`（模块级常量，注释：美股休市日，手工维护；放在 expiry.py 里，不改 config.py）
  - `cn_events(months: list[tuple[int, int]], trade_days: list[str]) -> list[dict]`：每月两条：
    `{"date", "market": "A股", "kind": "index", "name": "股指期货、期权交割日", "quarterly": m in (3,6,9,12)}`（第三个周五；不在 `trade_days` → 顺延到下一个交易日）；
    `{"date", "market": "A股", "kind": "etf_option", "name": "ETF 期权到期日", "quarterly": False}`（第四个周三，同样顺延）。
  - `us_events(months: list[tuple[int, int]], holidays: set[str]) -> list[dict]`：第三个周五，在 holidays 内 → 往前找最近的非周末非假日；季月 `name = "四巫日（股指期货、期权同日到期）"`，其余 `"月度期权到期日"`；`market: "美股"`、`kind: "us"`、`quarterly` 同上。
  - `reminders(events: list[dict], day: str, next_days: list[str]) -> dict`：`next_days` = day 之后的 5 个 A 股交易日；返回 `{"today": [当天的事件], "upcoming": [day 与 next_days 范围内（含 day）的事件，按日期升序]}`。美股事件按日期落在 `[day, next_days[-1]]` 自然日区间即纳入。
- [ ] **Step 1: 写失败测试**：
  - `test_cn_2026_11_12`：`trade_days` = 2026-11、12 月工作日（`pd.bdate_range`）→ 股指交割 `20261120`、`20261218`（quarterly True）；ETF 期权 `20261125`、`20261223`。
  - `test_cn_shift_holiday`：把 `20261120` 从 trade_days 去掉 → 股指交割日 `20261123`。
  - `test_us_2026_11_12`：`20261120` 月度、`20261218` 四巫日。
  - `test_us_holiday_moves_earlier`：2027-06 第三个周五 `20270618` 在 `expiry.US_HOLIDAYS` → `20270617`。
  - `test_reminders_window`：day `20261117`、next_days 5 个交易日到 `20261124` → upcoming 含 `20261120` 两条（A 股 + 美股），不含 `20261125`；day `20261120` → today 有 2 条。
- [ ] **Step 2:** 跑 → FAIL。
- [ ] **Step 3:** 实现。
- [ ] **Step 4:** 全量测试绿。
- [ ] **Step 5:** 提交 `feat: 衍生品到期日推算`。

---

### Task 3: 取数与 payload 组装

**Files:**
- Modify: `sources.py`、`demo.py`、`build.py`、`summary.py`
- Test: `tests/test_build.py`、`tests/test_summary.py`、`tests/test_sources.py`、`tests/test_demo.py`

**Interfaces:**
- Consumes: Task 1、Task 2 全部接口与 config 常量。
- Produces：
  - `TushareSource.fut_mapping(self, code, start, end)` → `self._fetch("fut_mapping", ["trade_date", "mapping_ts_code"], ts_code=code, start_date=start, end_date=end)`；
  - `TushareSource.trade_days_between(self, start, end)` → SSE 开市日升序（`trade_cal` 同现有写法，可含未来日期）；
  - `DemoSource` 两个同签名方法：`fut_mapping` 每季（3、6、9、12 月第二个周五）换一次合约代码；`trade_days_between` 用 `_BDAYS`；所有新序列在 demo 下有 ≥ 2 年历史（`_BDAYS` 从 2024-01 起已够）；`DemoFred` 已有的序列不变。
  - `build.py::_liquidity_index(c: Ctx) -> dict`：取数窗口 `_shift(D, -GAUGE["lookback_days"])`；序列来源见 spec §6（DR001/DR007/R007 用 `src.repo`，政策利差用已有 `ml.policy_spread_bp`，换锚 break 日 = `POLICY_RATES` 中名称与上一行不同的生效日；`T.CFX`、`SC.INE` 的 break 日 = `fut_mapping` 合约代码变化的日期；FRED 三个序列直接 `c.fred.series(...)`，SOFR−IORB 用 `ml.spread_bp`）；返回 spec §7 结构（`chg5`：bp 类为 bp、`chg5_unit` = "bp"；price 类换成百分比 `(exp(chg)−1)×100`、`chg5_unit` = "%"），另加 `"gate"`（用已有 M1、社融、`fed_net` 序列，可从 `_liquidity` 传入或在此重算，二选一，避免重复调用接口即可）；`history` 取 D 往前 365 自然日内的 A 股交易日；`value`、`ago`、`history[].value`、`components[].score` 取整；`domestic/external` 保留 1 位小数。整体用 `_safe("流动性指数", …, default)` 包住，default 为 `{"value": None, …}` 的空结构。
  - `build.py::_calendar(c: Ctx) -> dict`：当月与下月的 `cn_events + us_events`，`reminders(...)`；`_safe` 包住，default `{"today": [], "upcoming": []}`。
  - payload 新增顶层 `liquidity_index`、`calendar`。
  - `summary.build_conclusions` 新增关键字参数 `liquidity_index: dict | None = None`；有值时流动性卡 `text = f"流动性{zone}（{value}）"`、`sub = reason`、`tone` 按 spec §5.3；`value is None` → `("流动性数据不足", …, "gray")`；参数为 None（旧调用）→ 保持现有投票逻辑不变。
- [ ] **Step 1: 写失败测试**：
  - `test_build.py::test_liquidity_index_payload`：demo 构建后 `li["value"]` 为 0–100 整数、`li["zone"]` 在五档内、`len(li["components"]) == 11`、`gold`/`sc_oil` 的 `scored is False and score is None`、`len(li["combos"]) == 3`、`li["ago"]` 有 `prev/w1/m1/y1` 四键且 `prev`、`w1`、`m1`、`y1` 均非 None（demo 有 2 年历史）、`li["history"]` ≥ 240 条。
  - `test_build.py::test_gauge_fred_down`：`DownFred`（已有替身）→ `li["value"]` 非 None，`li["reason"]` 以「（外部缺 2 项）」结尾。
  - `test_build.py::test_calendar_payload`：`--date 20261117` → `calendar.upcoming` 含 `20261120` 的 A 股与美股两条；`--date 20261120` → `calendar.today` 非空。
  - `test_build.py::test_liquidity_conclusion_from_index`：流动性结论卡 `text` 以「流动性」开头并含「（」+ 指数 +「）」。
  - `test_summary.py`：`liquidity_index` 三种情况（偏紧 → green；None value → gray；不传 → 旧投票文案不变）。
  - `test_sources.py`：`fut_mapping`、`trade_days_between` 的列与排序（用已有的 fake `call` 写法）。
- [ ] **Step 2:** 跑 → FAIL。
- [ ] **Step 3:** 实现。
- [ ] **Step 4:** 全量测试绿；演示构建 `.venv/bin/python build.py --demo --out site/index.html --date 20261106` 退出 0、0 条警告。
- [ ] **Step 5:** 提交 `feat: 流动性指数与到期日提醒接入构建`。

---

### Task 4: 页面

**Files:**
- Modify: `template.html`、`build.py`（仅 `ratio_note` 文案）、`README.md`
- Test: `tests/test_render.py`（若无则新建；只做字符串级检查）

**Interfaces:**
- Consumes: payload `liquidity_index`、`calendar`（Task 3）。
- Produces（前端函数名供评审对照）：`gaugeSvg(value, opts)`（移植草图 `gauge()`，`opts.mini` 无刻度无档位文字）、`renderGaugeHero(p)`、`renderGaugeComponents(p)`、`renderReminders(p)`、`expiryNote(kind)`（返回当天匹配事件的短注释或空串）。

- [ ] **Step 1: 流动性标签页** `renderLiquidity` 改为（spec §5.1）：
  1. `renderGaugeHero`：卡片左「流动性指数」+ 副标题「现在市场的钱是松还是紧？」+ `gaugeSvg` + `reason` 一句 + 三个组合标签（`on` 高亮，文字「{name} · 触发 {met}/3」或「{name} · 未触发」）+ 闸门 notes（每条一行黄色提示）；右栏四行历史（上一交易日 / 1 周前 / 1 个月前 / 1 年前，档位文字 + 圆圈分数，None → 「—」）。`liquidity_index` 缺失或 `value` 为 None → 卡片里只写「流动性指数：数据不足」。
  2. `renderGaugeComponents`：分组表「国内 / 外部 / 参考（不计分）」；列「指标（小字 hint）/ 最新 / 近 5 日 / 分项得分条 / 得分」；近 5 日按 `chg5_unit` 显示（bp 取 0 位、% 取 2 位，带正负号与涨跌色）；参考组得分列「—」。**不显示 z。**
  3. 「场内资金」：原 `renderVote` + `cardGrid(L.domestic)`，标题「场内资金」。
  4. `<details>`（默认收起，summary 文案「更多数据」）：`cardGrid(L.macro)` 与 `cardGrid(L.external)`，**过滤掉** key 为 `m1, m2, sf, fed_net, reserves, tga, rrp` 的卡；`renderRate`；政策工具表。
  5. 去掉流动性页里的 `renderIndices`、`renderAssets` 调用；新闻块保持原条件。
- [ ] **Step 2: 总览**：`ovLiquidity` 改为小表盘卡（标题「流动性」+ 档位徽章 + `gaugeSvg(v, {mini:true})` + 一行「{reason 去掉末尾括注} · 较上一交易日 ±N · 较 1 周前 ±N」，缺项省略）；`liquidity_index` 缺失时回退旧卡片。`renderHome` 末尾（四卡之后）调用 `renderReminders`：`calendar.upcoming` 为空 → 不渲染；每行「M/D（周X）{market} {name}{quarterly ? "（季月）" : ""}」，today 的行加粗并在后面写「今天：成交与尾盘波动可能放大」。
- [ ] **Step 3: 国家队页注释**：观察池天量表格上方、总览国家队卡的天量提示行末尾、尾盘占比旁，若 `calendar.today` 有 `etf_option`（对天量）或 `index`（对尾盘与天量）事件，追加小字「当天为 ETF 期权到期日」/「当天为股指交割日」。
- [ ] **Step 4: 去掉页面上的公式文字**（改成不含算法的说明或直接删）：
  - `template.html:940` 「每日净申赎（亿元），正为流入；净申赎 = Δ份额 × 当日收盘价」→「每日净申赎（亿元），正为流入」
  - `template.html:1052` 「仅 510300 一只 14:30 后成交 ÷ 全天」→「代表 ETF：510300」
  - `template.html:1119` 观察池副标题 →「{N} 只 · 不计入上面的判断」
  - `template.html:1268` 浓度墙口径段落 →「黑色竖刻度 = 昨日值」
  - `build.py:346` `ratio_note` →「较 20 日均 {x:.2f} 倍」
  - 页面其他地方若还有 `÷`、`σ`、`标准差`、`Σ` 一并处理。方法说明移入 README 对应小节。
- [ ] **Step 5: README**：新增「流动性指数」小节（spec §4 全部规则、`config.POLICY_RATES` 与 `expiry.US_HOLIDAYS` 需手工维护、页面不显示方法的约定）与「到期日提醒」小节；把 Step 4 删掉的口径说明搬进对应小节。
- [ ] **Step 6: 检查**：
  - `tests/test_render.py::test_no_method_text_in_template`：`template.html` 去掉 `<script>` 里的注释后，不含 `σ`、`标准差`、`÷`、`Σ`；
  - 演示构建、真实旧存档 `history/2026-11-06.json` 渲染都成功；用浏览器（playwright 或本机 Chrome 均可）打开 demo 页的 `#home`、`#liquidity`、`#team`，控制台无报错；375px 宽无横向滚动；
  - 全量 pytest 绿。
- [ ] **Step 7:** 提交 `feat: 流动性表盘页面、到期日提醒、页面去公式`。

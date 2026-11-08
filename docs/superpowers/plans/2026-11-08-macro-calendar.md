# 宏观数据卡片 + 财经日历页 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal：** 在看板里加入两部分：一是中美 6 张宏观卡片（CPI、PPI、非农、失业率），带走势小图和「下次公布」时间；二是一页「日历」，以月历形式展示中美关键数据的公布时间、前值、预期和公布值。

**Architecture：**
- 纯逻辑放进新模块 `econ_calendar.py`，负责白名单匹配、清洗、去重、合并、计算下次公布，以及分段取数。
- 取数接口加在 `sources.py`、`fred.py`、`demo.py`。
- `build.py` 读写 `data/eco_cal.json` 做增量存储，生成 payload 的 `eco_calendar`，并给卡片加上 note。
- `template.html` 新增 `calendar` 页面。
- workflow 加上早上那一次 cron，存档时把 `data/eco_cal.json` 一起提交。

**Tech Stack：** Python 3.12、pandas、tushare、FRED API（urllib）、pytest；单文件 HTML，原生 JS（日历页不用 ECharts）。

**Spec：** `docs/superpowers/specs/2026-11-08-macro-calendar-design.md`（执行者必读，所有「为什么」都在里面）

## Global Constraints

- tushare token 只从环境变量 `TUSHARE_TOKEN` 读取，FRED key 只从 `FRED_API_KEY` 读取。两者都不能打印、写日志、写进 HTML 或提交到仓库；出错时日志只记异常类型。
- 不新增依赖，`requirements.txt` 保持不变。
- 只展示事实。不做好坏判断、不投票、不打分，不用颜色暗示利多或利空。日历页的圆点只用中性色：中国用 `C.warm`，美国用 `C.cold`；不用 `C.up` 或 `C.down`。
- 只处理中国和美国的事件。地区由白名单条目决定，**不使用 `eco_cal` 的 `country` 字段判断地区**（这个字段会标错）。
- 时间一律按北京时间。代码里显式使用 `build.SH`（Asia/Shanghai），不依赖环境变量 `TZ`。
- 「下次公布」宁可不写，也不能写错。找不到就不写这句话。
- 前端所有外部文本都必须经过 `el()` / `textContent` 渲染，不能用 innerHTML 拼接数据。
- 页面颜色和字体沿用现有的 CSS 变量，不引入粉色。
- 每个任务结束时，`pytest -q` 必须全部通过。

## Review Focus

1. **`eco_cal` 某段正好返回 100 条**（被截断了）：必须把区间对半切开重取，不能静默丢数据。由 Task 3 的 `test_fetch_splits_when_truncated` 覆盖。
2. **`data/eco_cal.json` 不存在、为空或损坏**：要从空数据开始，记一条 WARNING，构建照常完成。由 Task 3 的 `test_load_store_missing_or_corrupt` 覆盖。
3. **取数中途接口报错**（RateLimited、PermissionDenied 等）：已取到的部分要合并保存，旧数据保留，记 WARNING，不能整页失败。由 Task 3 的 `test_fetch_stops_on_error_keeps_partial` 覆盖。
4. **当天 09:30 的事件**：09:00 构建时它算「下次公布」；21:30 构建时它已公布，「下次公布」要跳到下一期。由 Task 1 的 `test_next_release_same_day_morning_vs_evening` 覆盖。
5. **没有 FRED key**（走 CSV 退路）：`release_dates` 返回 None，美国卡片不写「下次公布」，也不报错。由 Task 2 的 `test_release_dates_without_key_returns_none` 和 Task 4 的 `test_us_card_note_absent_when_no_schedule` 覆盖。

---

## 文件结构

| 文件 | 职责 |
|---|---|
| `econ_calendar.py`（新建） | 纯逻辑：匹配、时间清洗、期间提取、规整、去重、合并、下次公布、文案、分段取数、读写存档 |
| `config.py` | 新增 `ECO_CAL_*` 常量、`ECO_CAL_ITEMS` 白名单、`FRED_MACRO_SERIES`、`FRED_RELEASES`、`MACRO_SPARK_MONTHS` |
| `sources.py` | `TushareSource.eco_cal / cn_cpi / cn_ppi` |
| `fred.py` | `FredSource.release_dates` |
| `demo.py` | 上面这些方法的 demo 版本，以及 `DemoFred` 的新序列 |
| `build.py` | `_eco_calendar(c)`、6 张新卡、`collect` 新参数 `eco_path`、CLI 参数 `--eco-cal` |
| `template.html` | `calendar` 页面（月历、圆点、浮窗） |
| `.github/workflows/build.yml` | 新增 cron `0 1 * * 1-5`；`git add` 加上 `data/eco_cal.json` |
| `README.md` | 日历页说明、两次构建的作用、数据源和配额 |
| `tests/test_econ_calendar.py`（新建）、`tests/test_sources.py`、`tests/test_fred.py`、`tests/test_demo.py`、`tests/test_build.py` | 测试 |

---

### Task 1: 白名单与纯逻辑（`econ_calendar.py` 规整 / 去重 / 下次公布）

**Files:**
- Create: `econ_calendar.py`
- Modify: `config.py`（末尾追加）
- Test: `tests/test_econ_calendar.py`

**Interfaces:**
- Produces（后续任务直接使用）：
  - `config.ECO_CAL_ITEMS: list[dict]`，每项包含 `key: str`、`region: "cn"|"us"`、`label: str`、`pattern: str`（正则，用 `re.search` 匹配事件名）、`major: bool`、`kind: "data"|"event"`。
  - **事件 dict**，字段固定：`{"date": "YYYYMMDD", "time": "HH:MM"|None, "region": "cn"|"us", "item": str, "label": str, "period": str|None, "major": bool, "value": str|None, "pre": str|None, "fore": str|None}`。
  - `match_item(event_name: str) -> dict | None`：返回第一个命中的 `ECO_CAL_ITEMS` 项。
  - `clean_time(t: str | None, kind: str) -> str | None`
  - `period_of(event_name: str) -> str | None`
  - `normalize(rows: list[dict]) -> list[dict]`：输入 `eco_cal` 原始行（字段名与接口相同），输出事件 dict；不在白名单的行丢弃。
  - `dedupe(events: list[dict]) -> list[dict]`：后出现的视为更新的数据；结果按 `(date, time or "99:99", region, item)` 排序。
  - `merge(stored: list[dict], fresh: list[dict]) -> list[dict]`，等价于 `dedupe(stored + fresh)`。
  - `is_released(ev: dict, now: datetime) -> bool`
  - `next_release(events: list[dict], item: str, now: datetime) -> dict | None`
  - `format_next(date: str, time: str | None) -> str`

- [ ] **Step 1：在 `config.py` 末尾写白名单和常量**

`ECO_CAL_ITEMS` 的条目和正则如下。这些正则已经用 2026-09～11 的真实事件名核对过，照抄即可：

| key | region | label | pattern | major | kind |
|---|---|---|---|---|---|
| cn_cpi_yoy | cn | CPI 年率 | `^中国CPI年率` | True | data |
| cn_cpi_mom | cn | CPI 月率 | `^中国CPI月率` | False | data |
| cn_ppi_yoy | cn | PPI 年率 | `^中国PPI年率` | True | data |
| cn_m2 | cn | M2 年率 | `^中国M2货币供应年率` | True | data |
| cn_loans | cn | 新增人民币贷款 | `^中国新增人民币贷款` | True | data |
| cn_tsf | cn | 社会融资规模 | `^中国社会融资规模` | True | data |
| cn_pmi_mfg | cn | 官方制造业 PMI | `^中国官方制造业PMI` | False | data |
| cn_pmi_non | cn | 官方非制造业 PMI | `^中国官方非制造业PMI` | False | data |
| cn_gdp | cn | GDP 年率 | `^中国季度GDP年率` | True | data |
| cn_lpr | cn | LPR | `^中国央行贷款市场报价利率` | True | data |
| cn_ip | cn | 工业增加值年率 | `^中国规模以上工业增加值年率` | False | data |
| cn_retail | cn | 社零年率 | `^中国社会消费品零售总额年率` | False | data |
| us_cpi_yoy | us | CPI 年率 | `^美国未季调CPI年率` | True | data |
| us_cpi_mom | us | CPI 月率 | `^美国CPI月率` | False | data |
| us_core_cpi_yoy | us | 核心 CPI 年率 | `^美国未季调核心CPI年率` | False | data |
| us_core_cpi_mom | us | 核心 CPI 月率 | `^美国核心CPI月率` | False | data |
| us_ppi_yoy | us | PPI 年率 | `^美国PPI年率` | True | data |
| us_ppi_mom | us | PPI 月率 | `^美国PPI月率` | False | data |
| us_nfp | us | 非农就业人数 | `^美国季调后非农就业人口变动` | True | data |
| us_unrate | us | 失业率 | `^美国失业率` | False | data |
| us_claims | us | 初请失业金人数 | `^美国初请失业金人数$` | False | data |
| us_pce_yoy | us | PCE 年率 | `^美国PCE物价指数年率` | False | data |
| us_core_pce_mom | us | 核心 PCE 月率 | `^美国核心PCE物价指数月率` | False | data |
| us_gdp | us | 实际 GDP 年化季率 | `^美国实际GDP年化季率` | False | data |
| us_fomc_rate | us | FOMC 利率决议 | `^美联储利率决议` | True | event |
| us_fomc_stmt | us | FOMC 货币政策声明 | `^美联储FOMC货币政策声明` | False | event |
| us_fomc_presser | us | FOMC 新闻发布会 | `^美国联邦公开市场委员会新闻发布会` | False | event |
| us_fomc_minutes | us | FOMC 会议纪要 | `(FOMC\|联邦公开市场委员会).*会议纪要` | True | event |
| us_powell | us | 鲍威尔讲话 | `鲍威尔` | False | event |

其他常量：

```python
ECO_CAL_PAST_DAYS = 14        # 每次构建刷新：今天往前
ECO_CAL_FUTURE_DAYS = 60      # 每次构建刷新：今天往后
ECO_CAL_KEEP_DAYS = 182       # 回填目标 + payload 保留：今天往前约 6 个月
ECO_CAL_MAX_CALLS = 30        # 单次构建 eco_cal 调用上限
ECO_CAL_CHUNK_DAYS = {"中国": 31, "美国": 7}   # 按 eco_cal 的 country 参数分段取数
ECO_CAL_ROW_LIMIT = 100       # 接口单次上限；返回正好 100 条视为可能被截断
```

- [ ] **Step 2：写失败测试 `tests/test_econ_calendar.py`**

样本直接用 spec 里记录的真实数据，构造成原始行 dict（`date, time, country, event, value, pre_value, fore_value`）：

```python
def test_match_ignores_country_and_drops_noise():
    assert match_item("英国贸易帐(英镑)(十月)") is None            # 原数据 country=中国
    assert match_item("美国当周EIA原油库存变动(桶)") is None
    assert match_item("美国初请失业金人数四周均值") is None
    assert match_item("美国U6失业率(%)(九月)") is None
    assert match_item("中国CPI年率(%)(年度)(十月)")["key"] == "cn_cpi_yoy"
    assert match_item("美国季调后非农就业人口变动(九月)")["key"] == "us_nfp"
    assert match_item("美国未季调核心CPI年率(%)(年度)(八月)")["key"] == "us_core_cpi_yoy"

def test_clean_time_placeholders_only_for_data():
    assert clean_time("02:38", "data") is None
    assert clean_time("23:17", "data") is None
    assert clean_time("09:30", "data") == "09:30"
    assert clean_time("09:45", "data") == "09:45"
    assert clean_time("21:05", "event") == "21:05"   # 讲话类保留原时间（裁定见 Task 1 Step 3）
    assert clean_time(None, "data") is None and clean_time("", "event") is None

def test_period_of():
    assert period_of("中国M2货币供应年率(%)(年度)(九月)") == "九月"
    assert period_of("中国季度GDP年率(%)(年度)(第三季度)") == "第三季度"
    assert period_of("中国央行贷款市场报价利率(LPR)") is None
    assert period_of("美联储利率决议") is None

def test_dedupe_reschedule_prefers_value():
    # 9 月 M2：10/14 有公布值，10/15 无公布值 → 只留 10/14
    ...
    assert [(e["date"], e["value"]) for e in out if e["item"] == "cn_m2"] == [("20261014", "7.2%")]

def test_dedupe_reschedule_no_value_keeps_latest_date():
    # 同 item 同期间、都没有公布值、日期相差 ≤45 天 → 留日期较晚的那条

def test_dedupe_same_period_far_apart_kept():
    # 同 item、期间都是 "十月"，日期相差 > 45 天（不同年份）→ 两条都保留

def test_dedupe_lpr_name_variants_same_day():
    # 10/20「中国央行贷款市场报价利率(LPR)」（value 3.00%）与「…(LPR)(十月)」（value 3.50%）同日
    # → 同日同 item 只留一条：都有值时取后出现的那条

def test_merge_fresh_value_overrides_stale_empty():
    stored = [ev(item="cn_cpi_yoy", date="20261109", value=None)]
    fresh = [ev(item="cn_cpi_yoy", date="20261109", value="0.8%")]
    assert merge(stored, fresh)[0]["value"] == "0.8%"

def test_merge_keeps_stored_when_fresh_missing():
    assert len(merge([ev(item="cn_lpr", date="20261020", value="3.50%")], [])) == 1

def test_next_release_same_day_morning_vs_evening():
    evs = [ev(item="cn_cpi_yoy", date="20261109", time="09:30"),
           ev(item="cn_cpi_yoy", date="20261209", time="09:30", period="十一月")]
    m = datetime(2026, 11, 9, 9, 0, tzinfo=SH); n = datetime(2026, 11, 9, 21, 30, tzinfo=SH)
    assert next_release(evs, "cn_cpi_yoy", m)["date"] == "20261109"
    assert next_release(evs, "cn_cpi_yoy", n)["date"] == "20261209"

def test_next_release_pending_time_counts_until_day_ends():
    # time=None、date=今天 → 未公布；date=昨天 → 已公布
    # 已有 value 的事件无论时间都算已公布

def test_format_next():
    assert format_next("20261109", "09:30") == "下次公布：11/9 周一 09:30"
    assert format_next("20261113", None) == "下次公布：11/13 周五（时间待定）"
```

`ev(...)` 是测试文件里的辅助函数，用来生成完整的事件 dict，未给出的字段填默认值。`SH = ZoneInfo("Asia/Shanghai")`。省略号部分按注释构造数据。

- [ ] **Step 3：运行，确认失败**

运行：`pytest tests/test_econ_calendar.py -q`，预期：ImportError / FAIL。

- [ ] **Step 4：实现 `econ_calendar.py`**

规则（spec 写过的不再重复，这里只列新增的裁定）：
- **`clean_time`**：`kind == "data"` 且分钟不在 {00, 15, 30, 45} 时返回 None。`kind == "event"`（讲话、FOMC）时，只要格式是 `HH:MM` 就原样保留。**裁定**：spec 原文写的是一律按分钟判断，但鲍威尔讲话常在 21:05 这类时刻，一律判断会误杀。占位时间目前只在中国贸易数据上出现过，而贸易数据不在白名单里。
- **`period_of`**：取事件名里最后一个括号的内容，且该内容要匹配 `^(一|二|三|四|五|六|七|八|九|十|十一|十二)月$` 或 `^第[一二三四]季度$`。
- **`label`**：显示用，取 `item.label`，有期间时加 ` · 十月`，例如 `"CPI 年率 · 十月"`。
- **`value` / `pre` / `fore`**：原样保留字符串；None、空串、`"nan"` 一律转成 None。
- **`dedupe` 分两轮**：
  1. 同一个 `(item, date)` 只留一条。
  2. 同一个 `item`、`period` 非空且相同、日期相差 ≤ 45 天的，只留一条。

  每轮的优先级：有 value > 在输入中出现得更晚 > 日期更晚。
- **`is_released`**：value 非空 → True；time 非空 → `(date, time) <= now`（转成北京时间比较）；time 为空 → `date < now 的日期`。
- **`next_release`**：在该 item 的事件里，取未公布的、`(date, time or "99:99")` 最小的那条，返回事件 dict；没有就返回 None。
- **`format_next`**：星期用 `周一…周日`；日期格式与前端 `fmtDay` 一致，写成 `11/9`。

- [ ] **Step 5：运行，确认通过**

运行：`pytest -q`，预期：全部 PASS。

- [ ] **Step 6：Commit**

```bash
git add econ_calendar.py config.py tests/test_econ_calendar.py
git commit -m "feat: 财经日历白名单与清洗、去重、下次公布逻辑"
```

---

### Task 2: 数据源（tushare `eco_cal` / `cn_cpi` / `cn_ppi`，FRED 公布日程，demo）

**Files:**
- Modify: `sources.py`（`social_financing` 之后）、`fred.py`、`demo.py`、`config.py`
- Test: `tests/test_sources.py`、`tests/test_fred.py`、`tests/test_demo.py`

**Interfaces:**
- Produces:
  - `TushareSource.eco_cal(self, start: str, end: str, country: str) -> DataFrame`：列为 `["date","time","country","event","value","pre_value","fore_value"]`，按 `(date, time)` 升序。调用 `self._fetch("eco_cal", cols, country=country, start_date=start, end_date=end)`。
  - `TushareSource.cn_cpi(self, start_m, end_m) -> DataFrame`：列 `["month","nt_yoy"]`，带 `fields="month,nt_yoy"`。
  - `TushareSource.cn_ppi(self, start_m, end_m) -> DataFrame`：列 `["month","ppi_yoy"]`，带 `fields="month,ppi_yoy"`。
  - `FredSource.release_dates(self, release_id: int, start: str) -> list[str] | None`：start 格式为 YYYY-MM-DD，返回 `YYYYMMDD` 升序列表。**没有 api_key 时直接返回 None，不发请求。** 失败时记 WARNING（只写异常类型）并返回 None。
  - `config.FRED_MACRO_SERIES = ["CPIAUCSL", "PPIFIS", "PAYEMS", "UNRATE"]`
  - `config.FRED_RELEASES = {"us_cpi": 10, "us_ppi": 46, "us_nfp": 50, "us_unrate": 50}`：分别对应 CPI、PPI、Employment Situation（非农和失业率共用）。有 FRED_API_KEY 时，可以用 `fred/release?release_id=` 核对一下名称；核对不了也照用，这三个 id 是公开的常用值。
  - `config.MACRO_SPARK_MONTHS = 24`
  - `DemoSource.eco_cal / cn_cpi / cn_ppi` 与上面同签名；`DemoFred.release_dates(release_id, start)` 同签名；`DemoFred._SPEC` 新增 4 个月度序列（频率 `"MS"`）。

- [ ] **Step 1：写失败测试**

- **`test_sources.py`**：用已有的 fake `pro` 写法。
  - `test_eco_cal_columns_and_order`：传入倒序的 3 行，返回升序，列名正确，调用参数包含 `country`、`start_date`、`end_date`。
  - `test_cn_cpi_ppi_columns`
- **`test_fred.py`**：
  - `test_release_dates_parses_json`：fake fetch 返回 `{"release_dates":[{"release_id":10,"date":"2026-12-10"},{"release_id":10,"date":"2026-11-13"}]}`，结果为 `["20261113","20261210"]`；请求 URL 包含 `include_release_dates_with_no_data=true`。
  - `test_release_dates_without_key_returns_none`：没有 key 时 fetch 一次都不被调用。
  - `test_release_dates_error_does_not_leak_key`：fetch 抛出包含 key 的异常时，日志里不出现 key，返回 None。
- **`test_demo.py`**：
  - `test_demo_eco_cal_has_whitelist_and_noise`：`DemoSource().eco_cal("20261101","20261130","中国")` 的结果里，既要有能被 `econ_calendar.match_item` 命中的行（CPI、PPI、M2），也要有噪声行（例如「中国外汇储备(美元)(十月)」），还要有一条 `time="02:38"` 的占位行。
  - `test_demo_eco_cal_respects_row_limit`：任意区间返回的行数都 ≤ 100，并且一周 7 天的美国区间要能返回 ≥ 1 条。
  - `test_demo_fred_macro_series_monthly`：4 个新序列都非空，索引是月初日期。
  - `test_demo_release_dates`：返回未来的日期列表。

- [ ] **Step 2：运行，确认失败**

运行：`pytest -q tests/test_sources.py tests/test_fred.py tests/test_demo.py`，预期：FAIL。

- [ ] **Step 3：实现**

- **FRED**：URL 为 `https://api.stlouisfed.org/fred/release/dates?release_id={id}&realtime_start={start}&include_release_dates_with_no_data=true&sort_order=asc&api_key={key}&file_type=json`。复用 `fred.py` 已有的 `_http_get` 和错误处理风格。
- **Demo 的 `eco_cal`**：按日期确定性生成，与 `_hash` 同一个 seed 规则。
  - 中国：每月 9 日 09:30 生成 CPI/PPI；每月 13 日 17:00 生成 M2/贷款/社融（不带公布值）；另加一条外汇储备噪声，以及一条 02:38 的贸易帐占位行。
  - 美国：每周四 21:30 初请；每月第一个周五 21:30 非农和失业率；每月 12 日 21:30 CPI；再加一条 EIA 噪声。
  - 公布值规则：早于 2026-11-08 的有 value，其余 value 为 None。
  - 只返回 `[start, end]` 区间内、且 country 匹配的行。
- **Demo 的 `cn_cpi` / `cn_ppi`**：照 `m2` 的写法生成月度数据。
- **`DemoFred._SPEC`**：
  - `"CPIAUCSL": ("MS", 310.0, 0.003, 280, 360)`
  - `"PPIFIS": ("MS", 145.0, 0.004, 120, 170)`
  - `"PAYEMS": ("MS", 159000.0, 0.001, 150000, 170000)`
  - `"UNRATE": ("MS", 4.1, 0.05, 3.0, 6.0)`，`UNRATE` 加进 `_ADDITIVE`，倍率为 1.0。

- [ ] **Step 4：运行，确认通过**

运行：`pytest -q`，预期：全部 PASS。

- [ ] **Step 5：Commit**

```bash
git add sources.py fred.py demo.py config.py tests/test_sources.py tests/test_fred.py tests/test_demo.py
git commit -m "feat: eco_cal、cn_cpi、cn_ppi 取数与 FRED 公布日程（含 demo）"
```

---

### Task 3: 分段取数、增量存储与 payload（`build._eco_calendar`）

**Files:**
- Modify: `econ_calendar.py`、`build.py`
- Test: `tests/test_econ_calendar.py`、`tests/test_build.py`

**Interfaces:**
- Consumes：Task 1 的 `normalize`、`merge`；Task 2 的 `src.eco_cal`。
- Produces:
  - `econ_calendar.load_store(path: str) -> dict`：返回 `{"covered_from": str|None, "events": list[dict]}`。文件不存在 → 返回空结构（不记 WARNING）；文件损坏或格式不对 → 返回空结构，并记 WARNING「财经日历存档无法读取，已从空数据开始」。
  - `econ_calendar.save_store(path: str, store: dict) -> None`：先写临时文件，再用 `os.replace` 替换；JSON 用 `ensure_ascii=False, indent=0`，事件按日期排序。
  - `econ_calendar.fetch_events(src, today: str, store: dict, max_calls: int) -> tuple[dict, int]`：返回新的 store 和本次调用次数。
  - `build.collect(src, fred, date, round_start, demo, now=None, eco_path: str | None = None)`：`eco_path` 为 None 时不读写文件，只取刷新窗口（demo、测试用）。
  - `Ctx` 新增字段 `eco_path: str | None = None`；`c.shared["eco_events"]` 是 `list[dict]`，供 Task 4 使用。
  - **payload 新增键 `"eco_calendar"`**：`{"today": "YYYYMMDD", "start": "YYYYMMDD", "events": [事件 dict...], "status": "ok"|"stale"|"failed"}`。
    - `start` = today − `ECO_CAL_KEEP_DAYS`。
    - `events` 只保留 date ≥ start 的事件。
    - status：`ok` 表示本次刷新窗口全部取到；`stale` 表示部分没取到、用了存档；`failed` 表示既没取到也没有存档。
    - **注意**：已有的 `"calendar"` 键是到期日提醒，不要动它。
  - CLI 参数 `--eco-cal PATH`，默认 `data/eco_cal.json`。`main` 只在非 demo 模式下把它传给 `collect`。

- [ ] **Step 1：写失败测试**

`tests/test_econ_calendar.py`（用一个记录调用的 fake src，`eco_cal(start, end, country)` 返回 DataFrame）：
- **`test_fetch_window_chunks`**：today=20261108，store 为空，max_calls 很大时：
  - 中国按 ≤31 天分段，美国按 ≤7 天分段；
  - 覆盖 `[today-14, today+60]` 和回填区间 `[today-182, today-15]`；
  - 各段首尾相接、不重叠；
  - 结束后 `covered_from == today-182`。
- **`test_fetch_splits_when_truncated`**：fake 对某段返回正好 100 行 → 该段被对半切开重取；单日仍然 100 行时记 WARNING，不再继续切。
- **`test_fetch_respects_max_calls_backfill_resumes`**：
  - 先用 max_calls=5 跑一次：刷新窗口优先，回填从新到旧，`covered_from` 只推进到实际取完的位置；
  - 再用新预算跑第二次：从上次的 `covered_from` 往前接着回填。
- **`test_fetch_stops_on_error_keeps_partial`**：fake 在第 3 次调用时抛 `RateLimited` → 停止取数，记 WARNING，前 2 段的结果合并进 store，store 里原有的事件仍在。
- **`test_load_store_missing_or_corrupt`**：分别用不存在的路径、空文件、`"not json"` 测试，都返回空结构；只有后两种情况记 WARNING。
- **`test_save_then_load_roundtrip`**

`tests/test_build.py`：
- **`test_eco_calendar_payload_demo`**：用 `run(tmp_path)` 跑 demo：
  - `data["eco_calendar"]["status"] == "ok"`；
  - events 非空，且全部 `region in ("cn","us")`；
  - 没有任何事件的 label 里出现「外汇储备」或「EIA」；
  - 原数据里 02:38 的那条（如果在白名单里）time 为 None。
- **`test_eco_calendar_persists(tmp_path)`**：直接调用 `build.collect(DemoSource(), DemoFred(), DATE, None, True, now=..., eco_path=str(tmp_path/"e.json"))`：
  - 文件被写出；
  - 第二次调用后，事件不重复（`(item, date)` 唯一）。
- **`test_eco_calendar_source_down_uses_store(tmp_path, monkeypatch)`**：
  - 先跑一次生成存档；
  - 再让 `eco_cal` 抛异常 → `status == "stale"`，events 与存档一致，`meta.warnings` 里有一条关于财经日历的告警。

- [ ] **Step 2：运行，确认失败**

- [ ] **Step 3：实现**

**`fetch_events` 的顺序**：
1. 先取刷新窗口：中国一组、美国一组，每组按 `ECO_CAL_CHUNK_DAYS` 切段。
2. 再回填：从 `min(covered_from or today-15, today-15)` 往前到 `today-182`，按段从新到旧。

**两条规则**：
- 每调用一次计数加 1，到 `max_calls` 就停。
- 段返回 ≥ `ECO_CAL_ROW_LIMIT` 行且跨度 > 1 天时，对半切开重取（切开的两次也计数）。

**错误处理**：任何异常都停止整个取数，记 WARNING「财经日历取数中断（{类型}），已用存档补齐」，然后返回当前结果。所有取到的行先经过 `normalize` 再 `merge`。

**`build._eco_calendar(c) -> dict`**：
- 处理顺序：`load_store`（`eco_path` 为 None 时用空 store），然后 `fetch_events`，然后 `save_store`（仅当 `eco_path` 不为 None），最后组装 payload，并写入 `c.shared["eco_events"]`。
- 在 `_collect` 里的调用位置：放在 `liquidity` 之前，用 `_safe("财经日历", …, lambda: {"today": …, "start": …, "events": [], "status": "failed"})` 包住。

- [ ] **Step 4：运行，确认通过**

运行：`pytest -q`，预期：全部 PASS。并且 `python build.py --demo --out /tmp/d.html` 能成功生成页面。

- [ ] **Step 5：Commit**

```bash
git add econ_calendar.py build.py tests/test_econ_calendar.py tests/test_build.py
git commit -m "feat: 财经日历分段取数、增量存档与 payload"
```

---

### Task 4: 6 张宏观卡片与「下次公布」

**Files:**
- Modify: `build.py`（`_liquidity` 的 `macro` 列表、`_external` 的 `cards` 列表）
- Test: `tests/test_build.py`

**Interfaces:**
- Consumes：
  - `c.shared["eco_events"]`（Task 3）
  - `econ_calendar.next_release` / `format_next`（Task 1）
  - `src.cn_cpi / cn_ppi`、`c.fred.series`、`c.fred.release_dates`、`config.FRED_MACRO_SERIES / FRED_RELEASES / MACRO_SPARK_MONTHS`（Task 2）
  - 已有的 `ml.yoy_pct`
- Produces：
  - `liquidity.macro` 在 `"sf"` 之后插入 `"cn_cpi"`（`CPI同比`，%）、`"cn_ppi"`（`PPI同比`，%）。
  - `liquidity.external` 在 `"tips10"` 之后插入：
    - `"us_cpi"`（`美国CPI同比`，%）
    - `"us_ppi"`（`美国PPI同比`，%）
    - `"us_nfp"`（`美国非农新增`，千人）
    - `"us_unrate"`（`美国失业率`，%）
  - 6 张卡都用 `spark_n=config.MACRO_SPARK_MONTHS`，卡片的 `date` 为 `YYYYMM`。
  - helper `build._next_note(c, item: str, fred_key: str | None) -> str`：
    - 优先查日历：`next_release(c.shared.get("eco_events", []), item, c.now)`。
    - 日历里没有、但给了 fred_key 时：用 `c.fred.release_dates(config.FRED_RELEASES[fred_key], today)` 取第一个 ≥ 今天、并且晚于该卡最新数据所在月份的日期，格式化为 `format_next(date, None)`。
    - 都没有就返回 `""`。
    - 同一个 release_id 在一次构建中只请求一次（缓存在 `c.shared`）。
  - item 与卡片的对应：
    - `cn_cpi` ← `cn_cpi_yoy`
    - `cn_ppi` ← `cn_ppi_yoy`
    - `us_cpi` ← `us_cpi_yoy` / `"us_cpi"`
    - `us_ppi` ← `us_ppi_yoy` / `"us_ppi"`
    - `us_nfp` ← `us_nfp` / `"us_nfp"`
    - `us_unrate` ← `us_unrate` / `"us_unrate"`

- [ ] **Step 1：写失败测试**

- **`test_build_demo_end_to_end`**：更新其中 `macro` / `external` 两处 key 列表的断言，加入新 key。
- **`test_macro_cards_monthly(tmp_path)`**：用 demo 跑：
  - 6 张卡的 value 都不是 None；
  - `date` 匹配 `^\d{6}$`；
  - `len(spark) <= 24`；
  - `us_nfp` 的 unit 为 `"千人"`。
- **`test_card_next_note_from_calendar(tmp_path)`**：demo 跑，`now` 设为 2026-11-08 20:00，`cn_cpi` 卡的 note 以 `"下次公布：11/9 周一 09:30"` 开头（demo 的 eco_cal 里有 11/9 09:30 的 CPI）。
- **`test_us_card_note_falls_back_to_fred(tmp_path, monkeypatch)`**：
  - 让 demo 的 eco_cal 不返回美国 CPI；
  - `DemoFred.release_dates` 返回 `["20261210"]`；
  - 预期 `us_cpi` 的 note == `"下次公布：12/10 周四（时间待定）"`。
- **`test_us_card_note_absent_when_no_schedule(tmp_path, monkeypatch)`**：eco_cal 和 release_dates 都没有数据 → note 为 `""`。

- [ ] **Step 2：运行，确认失败**

- [ ] **Step 3：实现**

- **FRED 新序列**：单独一个循环，起始日为 `_month_start(D, 37)`，转换成 `YYYY-MM-DD`；不要并进已有 `FRED_SERIES` 的 400 天窗口。
- **月度索引**：转换成 `YYYYMM` 字符串后再交给 `ml.yoy_pct` 或 `_mk_card`。
- **非农新增**：`PAYEMS.diff()`，单位已经是千人。
- **中国两项**：用 `_ser(df, "month", "nt_yoy")` 和 `_ser(df, "month", "ppi_yoy")`，取数窗口为 `_month_start(D, 25)` 到 `D`。

- [ ] **Step 4：运行，确认通过**

运行：`pytest -q`，预期：全部 PASS。

- [ ] **Step 5：Commit**

```bash
git add build.py tests/test_build.py
git commit -m "feat: 中美 CPI、PPI、非农、失业率卡片与下次公布时间"
```

---

### Task 5: 日历页（`template.html`）

**Files:**
- Modify: `template.html`（CSS、`<section>`、`PAGES`、新增 `renderCalendar`）
- Test: `tests/test_render.py`（静态检查），以及浏览器实测

**Interfaces:**
- Consumes：payload 里的 `DATA.eco_calendar`（Task 3）。
- Produces：页面 key 为 `calendar`，名称为「日历」，放在 `PAGES` 里 `value` 之后；对应 `<section class="page" id="p-calendar">`。

- [ ] **Step 1：写静态测试** `test_calendar_page_wired`

断言 template 源码中同时包含 `id="p-calendar"`、`key: "calendar"`、`function renderCalendar`，以及文案 `数据来源 tushare eco_cal，时间为北京时间；预告时间以官方发布为准。`；并且 `renderCalendar` 函数体里不出现 `innerHTML`。

- [ ] **Step 2：运行，确认失败**

- [ ] **Step 3：实现 `renderCalendar(p)`**

行为（spec「日历页」一节是权威，这里只列具体取值）：

- **月份范围**：
  - 最早：`DATA.eco_calendar.start` 所在月；
  - 最晚：`max(最后一个事件所在月, today 所在月)`。
  - 初始显示 today 所在月。
  - 左右箭头按钮到了边界就 `disabled`。
  - 标题「2026 年 11 月」用原生 `<select>` 实现，选项是范围内的所有月份，选中即跳转。
- **网格**：
  - 7 列，表头依次是「一 二 三 四 五 六 日」；
  - 月初前、月末后的空位留白；
  - 日期格是 `<button>`；没有事件的日期设 `disabled` 且不响应点击。
- **今天**：日期数字加 2px 的 `var(--ink)` 圆环。
- **圆点**：
  - 中国 `C.warm`，美国 `C.cold`，两国都有就并排两个点；
  - 当天该国有 `major` 事件时圆点直径 7px，否则 5px。
- **浮窗**：
  - 标题为「11月13日 周五」；
  - 按 `(time or "99:99")` 排序，每行包含：
    - 时间（或「待定」）
    - 「中」/「美」小标签（用 `badge`，tone 为 gray）
    - `label`
    - 「前值 x · 预期 y · 公布 z」，空值显示 `—`
  - 有 `value` 的行，公布值加粗；
  - 右上角有关闭按钮；
  - 点击浮窗外部或按 Esc 关闭；
  - 宽度 > `NARROW`（560px）时，浮窗绝对定位在所点日期格的下方，并且要夹在日历容器之内；
  - 宽度 ≤ 560px 时，改成 `position: fixed` 的底部弹层，宽 100%，`max-height: 70vh`，内容可滚动。
- **页脚**：用小字显示 Step 1 里的那句来源文案。
- **status 的两种特殊情况**：
  - `status === "stale"`：标题下方加一行 `note`「本次未能刷新，显示的是存档数据」。
  - 没有任何事件：显示 `empty-note`「日历数据暂缺」。

- [ ] **Step 4：浏览器实测**

1. 运行 `python build.py --demo --out /tmp/cal.html`，用 Browser pane 打开 `file:///tmp/cal.html#calendar`。
2. 桌面宽度下检查：翻月、`<select>` 跳月、点有事件的日子弹出浮窗、Esc 或点外部能关闭、今天有圆环、两种颜色的圆点、大小点都正常。
3. 切到 mobile 预设（375 宽）后检查：浮窗是底部弹层，没有横向滚动。
4. 检查 console 没有报错。

- [ ] **Step 5：运行 `pytest -q`，确认全部 PASS**

- [ ] **Step 6：Commit**

```bash
git add template.html tests/test_render.py
git commit -m "feat: 日历页（月历、圆点、事件浮窗）"
```

---

### Task 6: workflow 早上一次构建、存档提交、README

**Files:**
- Modify: `.github/workflows/build.yml`、`README.md`
- Test: `tests/test_config.py`（若已有 workflow 相关检查则在那里加；没有就新增 `test_workflow_schedule`，用文本断言）

**Interfaces:**
- Consumes：Task 3 的 CLI `--eco-cal`（默认 `data/eco_cal.json`）。

- [ ] **Step 1：写失败测试 `test_workflow_schedule`**

读取 `.github/workflows/build.yml` 的文本，断言：
- 包含 `cron: "30 13 * * 1-5"` 和 `cron: "0 1 * * 1-5"`；
- 存档步骤里 `git add` 那一行同时包含 `history` 和 `data/eco_cal.json`。

- [ ] **Step 2：运行，确认失败**

- [ ] **Step 3：修改 workflow 与 README**

**workflow**：
- 新增一条 cron，注释写「每个工作日 09:00 北京时间：补上前一晚美国数据」；
- 存档步骤改为 `git add history data/eco_cal.json`；
- 存档步骤的 `name` 改为「存档当天数据与财经日历」；
- 构建命令不变（`--eco-cal` 用默认值）。

**README**：新增「财经日历」一节，内容包括：
- 数据源 `eco_cal`、白名单写在 `config.ECO_CAL_ITEMS`；
- 清洗规则：国家按事件名判定、去重、占位时间的处理；
- 存档文件 `data/eco_cal.json` 的作用；
- 回填半年时按 `ECO_CAL_MAX_CALLS` 限额分几次完成；
- 两次构建各自的作用，以及美国数据最晚在下一个工作日早上进入看板。

在「宏观」卡片清单里补上 6 张新卡及其数据源，并说明 FRED 公布日程的退路。

- [ ] **Step 4：运行 `pytest -q`，确认全部 PASS**

- [ ] **Step 5：Commit**

```bash
git add .github/workflows/build.yml README.md tests/test_config.py
git commit -m "ci: 工作日早上加一次构建，存档财经日历；README 说明"
```

---

## 收尾（控制者执行，不派给实现者）

- [ ] **真实数据核对**：用 tushare MCP 或本机 token 取 2026-09-01～11-30 的中美 `eco_cal`，跑一遍 `normalize` + `dedupe`，人工看一遍结果，要求：
  - 白名单每一项至少命中 1 条；美国 CPI 和非农如果确实缺失，要在 README 里注明；
  - 没有混入噪声；
  - 9 月 M2 只剩 1 条。
- [ ] **全分支 review**：在最强模型上做一次全分支审查。
- [ ] **推送和手动触发 workflow**：都要先征得用户同意。推送后：
  - 首次构建会开始回填半年数据；
  - 核对线上「日历」页和 6 张卡片；
  - 等第二天早上 09:00 那次构建跑完，确认它没有新增告警。

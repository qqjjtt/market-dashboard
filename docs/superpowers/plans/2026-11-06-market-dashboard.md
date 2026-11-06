# A 股盘后市场面板 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 一条命令（`python build.py`）用 tushare + FRED 生成单文件 HTML 面板，回答「流动性 / 板块流向 / 国家队 / 散户情绪」四个问题；GitHub Actions 每个交易日三次自动构建并发布到 GitHub Pages。

**Architecture:** 数据源层（`sources.py` TushareSource、`fred.py` FredSource、`demo.py` 同接口的随机源）只做 IO + 单位换算 + 重试降级；计算层（`metrics_*.py`、`summary.py`）全是纯函数；`build.py` 取数 → 计算 → 组装一个 payload dict → `render.py` 把 JSON 嵌进 `template.html`。前端只读 payload，不做口径计算。

**Tech Stack:** Python 3.12（本机 3.14 也须能跑）、tushare、pandas、numpy、pytest；ECharts 5.5.0（cdnjs）；GitHub Actions + Pages。

**Spec:** `docs/superpowers/specs/2026-11-06-market-dashboard-design.md`（下称 SPEC），它引用 `docs/reference/PROMPT.md`（下称 PROMPT）。冲突时 SPEC 优先。**每个任务开工前都要读 SPEC 中与本任务相关的章节。**

## Global Constraints

- 项目根目录：`/Users/qijuntong/Documents/market-dashboard`，所有路径相对它
- 运行依赖只允许 `tushare`、`pandas`、`numpy`；测试依赖 `pytest`；FRED 用标准库 `urllib.request`
- token 只从环境变量 `TUSHARE_TOKEN` 读；不得出现在代码、日志、HTML、测试夹具、仓库任何文件
- 红 = 涨 / 流入，绿 = 跌 / 流出；配色 `#c8402f` 红、`#e08a2a` 橙、`#2f5d8a` 蓝、`#2a9d75` 绿、`#8a8178` 灰字；背景 `#f6f1ea`，卡片 `#fffdf9`，描边 `#ece3d6`，圆角 14px
- 缺失值一律 `NaN`/`None` → JSON `null` → 前端「—」；**永不填 0**，未披露 ≠ 净赎回
- 所有「今天 / 生成时间」按 `Asia/Shanghai`（Actions 机器是 UTC）
- ECharts 只从 `https://cdnjs.cloudflare.com/ajax/libs/echarts/5.5.0/echarts.min.js` 加载，其余 CSS/JS 内联
- 嵌入 JSON：`</` → `<\/`，NaN → `null`；模板占位符字面量 `/*__DATA__*/null`
- 页面上凡是来自外部的文字（新闻标题、来源、ETF 名称）一律 `textContent` 插入，不拼 `innerHTML`
- 日志用 `logging`：INFO 打进度，WARNING 打缺失与降级；WARNING 同时收集进 `payload["meta"]["warnings"]`
- 70%–90% 归因比例在页面上必须带「归因比例为假设，非公开数据」
- 提交信息结尾加一行 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`

## Review Focus

1. **数据还没出来就跑**（16:30 时 moneyflow/板块流向/分钟线当天为空）→ 对应面板回退到该数据最近可用交易日并标注日期，或显示「待更新」，构建不失败 —— Task 8 `test_build_when_moneyflow_not_ready`
2. **某只 ETF 整只取不到**（新上市、代码停用）→ WARNING 列出代码并跳过，标题只数「实际取到的只数」—— Task 8 `test_build_skips_missing_etf`
3. **新闻标题含 `<script>` / `</` / 引号** → 原样显示为文字，不破坏页面 —— Task 8 `test_render_escapes_script_close`（JSON 层）+ Task 9 浏览器检查（DOM 层）
4. **FRED 不可达或返回非 CSV**（被墙、HTML 错误页）→ 外围卡片「无数据」，其余面板照常 —— Task 4 `test_fred_non_csv_returns_none`
5. **major_news 一个窗口正好 800 条（被截断）** → 自动二分窗口直到每段 < 800，结果去重 —— Task 3 `test_major_news_splits_full_window`

---

## 文件结构

| 文件 | 职责 |
|---|---|
| `config.py` | SPEC/PROMPT 全部可配置项、阈值、关键词、板块定义 |
| `sources.py` | `TushareSource`：节流、重试、权限识别、单位换算、新闻切片 |
| `fred.py` | `FredSource`：FRED 公开 CSV |
| `demo.py` | `DemoSource`、`DemoFred`：与上面两个类方法签名完全一致的随机数据 |
| `metrics_etf.py` | 净申赎、80% 披露、本轮、月度、成交指纹、尾盘、结论文案 |
| `metrics_sentiment.py` | 涨跌停阈值、体温、板块匹配、韭菜浓度墙 |
| `metrics_liquidity.py` | 卡片、流动性投票、美联储净流动性、加减息预期、板块流向排名、新闻过滤、政策表 |
| `summary.py` | 四句结论 |
| `render.py` | JSON 序列化与模板填充 |
| `build.py` | CLI、交易日判定、取数编排、payload 组装 |
| `template.html` | 页面 |
| `data/policy_ops.csv` | 手工维护的政策工具表 |
| `scripts/smoke_tushare.py` | Actions 冒烟测试 |
| `.github/workflows/build.yml` | 定时构建 + Pages 发布 |
| `tests/test_*.py` | 每个模块一份 |

## payload 契约（Task 8 产出，Task 9/10 消费）

```text
meta:        {trade_date, generated_at, demo, sector_mode, tail_source, warnings:[str]}
conclusions: [{key: liquidity|sector|team|retail, title, text, sub, tone: red|orange|blue|green|gray}] ×4
indices:     [{code, name, close, pct_chg}]
liquidity:   {domestic:[card], macro:[card], external:[card], vote:{score,label,signals:[{name,dir,detail}]},
              rate_expect:{effective, implied, diff_bp, label}}
policy_ops:  [{date, tool, amount, cum, url, note}]
news:        {domestic:[news], external:[news]}        news = {time, src, title, url, flag:bool}
sector_flow: {date, day:{inflow:[row], outflow:[row]}, d5:{inflow, outflow}}   row = {name, net, pct}
etf_flow:    {dates:[], daily:[], cum:[], index_close:[], per_etf:[{code, name, share:[]}]}
round:       {count, start, end, total, low, high, mid, last3:[{date, value}]}
monthly:     [{label:"11月", days, value}]
fingerprint: {date, groups:[{key,name,n,today,yday,chg_pct,ratio}], tail:{today,yday,source},
              conclusion:{big, small, text}, footnote}
temperature: {series:[{date, score}], kpis:{...}, dims:{...}, limit_up, limit_down, range, six_dim}
concentration: {date, market, market_yday, mode, rows:[{icon,name,n,today,yday,chg,color}]}
card = {key, label, value, unit, change, change_unit, date, spark:[], note, tone}
```

---

### Task 1: 项目骨架与配置

**Files:**
- Create: `.gitignore`, `requirements.txt`, `pytest.ini`, `config.py`, `tests/test_config.py`

**Interfaces:**
- Produces: `config` 模块常量 —— `CORE5, BROAD10, INDUSTRY6, SH11, FIXED_BROAD, DIVERGENCE_INDEX, ROUND_START, ATTRIBUTION, FLOW_MONTHS, MONTHLY_MONTHS, RETAIL_LEVELS, RECENT_LIMIT_UP_DAYS, TEMP_WINDOW, AMOUNT_MA, TAIL_START`（值照抄 PROMPT §3）；`SECTORS: list[dict(name, icon, sw:list[str], basic:list[str], dynamic:bool)]`（PROMPT §3 表 14 行，「高位连板」`dynamic=True`）；`A_INDICES = [("000001.SH","上证指数"),("399001.SZ","深证成指"),("399006.SZ","创业板指"),("000688.SH","科创50"),("000300.SH","沪深300")]`；`US_INDICES = [("SPX","标普500"),("IXIC","纳指"),("DJI","道指")]`；`VOTE = dict(turnover_hi=1.10, turnover_lo=0.90, margin_pct=0.5, shibor_bp=5, loose=2, tight=-2)`；`RATE_BAND_BP = 10`；`NEWS_DAYS = 7`；`NEWS_KW_DOMESTIC`、`NEWS_KW_EXTERNAL`（照抄 SPEC §4.1d）；`FRED_SERIES = ["WALCL","WTREGEN","RRPONTSYD","WRESBAL","DFF"]`；`MAX_CALLS_PER_MIN = 200`；`DISCLOSURE_MIN = 0.8`；`SPARK_DAYS = 30`

- [ ] **Step 1:** `git init`，`git config user.name qqjjtt`，`git config user.email qijunton.g@gmail.com`（仅本仓库）
- [ ] **Step 2: 写失败测试** `tests/test_config.py`

```python
import config
def test_lists():
    assert len(config.BROAD10) == 10 and config.BROAD10[:5] == config.CORE5
    assert len(config.FIXED_BROAD) == 29 and len(set(config.FIXED_BROAD)) == 29
    assert len(config.SH11) == 11 and all(c.endswith(".SH") for c in config.SH11)
def test_sectors():
    assert len(config.SECTORS) == 14
    assert [s["name"] for s in config.SECTORS if s["dynamic"]] == ["高位连板·情绪核心"]
    assert not any("恒生" in s["name"] for s in config.SECTORS)
```

- [ ] **Step 3:** `pytest -q` → FAIL（no module config）
- [ ] **Step 4:** 写 `config.py`；`requirements.txt` = `tushare>=1.4`、`pandas>=2.0`、`numpy>=1.26`、`pytest>=8.0`；`.gitignore` 含 `site/`、`__pycache__/`、`.venv/`、`*.log`；`pytest.ini` 设 `testpaths = tests`、`pythonpath = .`
- [ ] **Step 5:** `pytest -q` → PASS
- [ ] **Step 6:** 提交（含 `docs/` 已有的 SPEC、PROMPT、本计划）：`git add -A && git commit -m "chore: 项目骨架、配置与设计文档"`

---

### Task 2: GitHub 仓库与 Actions 冒烟测试（含用户操作，会停下等待）

**Files:**
- Create: `scripts/smoke_tushare.py`, `.github/workflows/smoke.yml`

**Interfaces:**
- Produces: 远端 `git@github.com:qqjjtt/market-dashboard.git`（或 HTTPS）可推送；结论「tushare 在 Actions 上可用 / 不可用」写进提交说明。不可用 → 停下报告用户，后续 Task 11 改走 SPEC §6 的 launchd 兜底

- [ ] **Step 1:** 写 `scripts/smoke_tushare.py`：读 `TUSHARE_TOKEN`，调 `trade_cal(exchange="SSE", is_open="1", start_date=<今天−10天>, end_date=<今天>)` 和 `daily(trade_date=<上一交易日>)`，打印两者行数，任一为 0 或异常则 `sys.exit(1)`；不打印 token
- [ ] **Step 2:** 本机跑 `python scripts/smoke_tushare.py` → 两个正行数
- [ ] **Step 3:** 写 `.github/workflows/smoke.yml`：仅 `workflow_dispatch`；`ubuntu-latest`；`actions/setup-python@v5` python 3.12；`pip install tushare pandas`；`env: TUSHARE_TOKEN: ${{ secrets.TUSHARE_TOKEN }}`；运行脚本
- [ ] **Step 4:** 提交
- [ ] **Step 5: 停下，给用户这份清单并等待完成**（实现者不经手 token）：
  1. 在 github.com 新建**公开**仓库 `market-dashboard`（不要勾选 README）
  2. 仓库 Settings → Secrets and variables → Actions → New repository secret：名 `TUSHARE_TOKEN`，值为 token
  3. Settings → Pages → Build and deployment → Source 选 **GitHub Actions**
  4. 推送凭据：实现者可在用户同意后运行 `ssh-keygen -t ed25519 -C qqjjtt -f ~/.ssh/id_ed25519 -N ""` 并把 `~/.ssh/id_ed25519.pub` 内容交给用户，用户在 github.com → Settings → SSH and GPG keys 添加
- [ ] **Step 6:** `git remote add origin git@github.com:qqjjtt/market-dashboard.git && git branch -M main && git push -u origin main`
- [ ] **Step 7:** 请用户在 Actions 页手动运行 smoke workflow（或无 gh 时由用户点 Run workflow），读取日志结果。PASS → 继续；FAIL → 停下报告

---

### Task 3: TushareSource

**Files:**
- Create: `sources.py`, `tests/test_sources.py`

**Interfaces:**
- Produces:
  - 异常：`PermissionDenied(Exception)`、`SourceError(Exception)`
  - `class TushareSource(pro, sleep=time.sleep, clock=time.monotonic, max_calls_per_min=config.MAX_CALLS_PER_MIN)`；`@classmethod from_env() -> TushareSource`
  - `call(api: str, **kw) -> pd.DataFrame`：节流 + 重试。错误消息含「没有接口」或「权限」→ 立即抛 `PermissionDenied`（不重试）；其他异常（含「频率超限」）等 2/4/8 秒重试，共 4 次，仍失败抛 `SourceError`
  - 下列方法统一返回 DataFrame（无数据返回空 DataFrame，**不是 None**）；`trade_date` 列一律 `str` YYYYMMDD：
    - `trade_days(end: str, n: int) -> list[str]`（≤ end 的最近 n 个开市日，升序）
    - `stock_daily(trade_date) -> [ts_code, pct_chg, amount]`（amount 亿元 = 千元 ÷ 1e5）
    - `stock_basic() -> [ts_code, name, industry]`
    - `moneyflow(trade_date) -> [ts_code, buy_sm_amount, sell_sm_amount, buy_md_amount, sell_md_amount, buy_lg_amount, sell_lg_amount, buy_elg_amount, sell_elg_amount]`（保持万元）
    - `sw_members() -> DataFrame | None`（`index_member_all(is_new="Y")` 按 limit=3000/offset 翻页；`PermissionDenied` → `None`）列 `[ts_code, l1_name, l2_name, l3_name]`
    - `etf_share(code, start, end) -> [trade_date, share]`（亿份 = fd_share 万份 ÷ 1e4）
    - `etf_daily(code, start, end) -> [trade_date, close, amount]`（amount 亿元）
    - `etf_names(codes: list[str]) -> dict[str, str]`（`fund_basic(market="E")`）
    - `index_daily(code, start, end) -> [trade_date, close, pct_chg]`
    - `etf_minutes(code, trade_date) -> DataFrame | None`（`stk_mins` freq=1min，09:30–15:00；列 `[trade_time, amount]`；`PermissionDenied` → None）
    - `sector_flow(start, end) -> [trade_date, industry, net_amount, pct_change]`（`moneyflow_ind_ths`，net_amount 亿元——实测量级为几十，实现时用文档核对并在 docstring 写明）
    - `market_flow(start, end) -> [trade_date, net_amount]`（`moneyflow_mkt_dc`，元 ÷ 1e8 → 亿元）
    - `margin(start, end) -> [trade_date, balance]`（`rzrqye` 三个交易所按日求和，元 ÷ 1e8 → 亿元）
    - `limit_counts(trade_date) -> dict(up:int|None, down:int|None)`（`limit_list_d` 的 `limit` 列 U/D 计数；空表 → None）
    - `inst_net_buy(trade_date) -> float | None`（`top_inst` 中 `exalter == "机构专用"` 的 `net_buy` 求和 → 亿元；单位按文档核对）
    - `shibor(start, end) -> [date, on, w1]`
    - `m2(start_m, end_m) -> [month, m2_yoy]`
    - `bond_fut_10y(start, end) -> [trade_date, close]`（`fut_daily(ts_code="T.CFX")`）
    - `fx(code, start, end) -> [trade_date, close]`（`fx_daily`，close = `bid_close`；用于 UKOIL / USDOLLAR / FED30D）
    - `global_index(start, end) -> [ts_code, trade_date, close, pct_chg]`
    - `us_yields(start, end) -> [date, y2, y10]`（`us_tycr`）
    - `major_news(start_dt: str, end_dt: str) -> [pub_time, src, title, url]`：初始 3 小时窗口；某窗口返回 ≥ 800 行就二分重取，直到 < 800 或窗口 ≤ 10 分钟；按 `(title)` 去重

- [ ] **Step 1: 写失败测试**（用假的 `pro` 对象，`sleep` 记录调用，`clock` 用可控计数器）

```python
def test_retry_backoff():            # 前两次抛 Exception("timeout")，第三次返回 df
    assert slept == [2, 4] and len(df) == 1
def test_retry_gives_up():           # 一直抛
    with pytest.raises(SourceError): src.call("daily", trade_date="20261105")
    assert slept == [2, 4, 8]
def test_permission_no_retry():      # 抛 Exception("抱歉，您没有接口(yc_cb)访问权限")
    with pytest.raises(PermissionDenied): ...
    assert slept == []
def test_throttle():                 # max_calls_per_min=2，clock 不前进时第 3 次调用前 sleep ≈ 60
def test_units():
    assert etf_share(...).share.iloc[0] == pytest.approx(1.2345)     # fd_share=12345 万份
    assert etf_daily(...).amount.iloc[0] == pytest.approx(1.23456)   # amount=123456 千元
    assert margin(...).balance.iloc[0] == pytest.approx(25.0)        # 两个交易所 rzrqye 各 1.25e9 元
def test_sw_members_permission_returns_none():
def test_major_news_splits_full_window():   # 假 pro：窗口跨度 > 90 分钟时返回 800 行，否则 10 行且含重复标题
    assert every_call_returned_lt_800_after_split and df.title.is_unique
```

- [ ] **Step 2:** `pytest tests/test_sources.py -q` → FAIL
- [ ] **Step 3:** 实现 `sources.py`。节流用滑动 60 秒窗口的调用时间队列
- [ ] **Step 4:** `pytest tests/test_sources.py -q` → PASS
- [ ] **Step 5:** 本机真实冒烟：`python -c "from sources import TushareSource as T; s=T.from_env(); print(len(s.stock_daily('20261105')), s.margin('20261101','20261105').tail(1))"` → 约 5500 行、两融余额约 2.5 万亿（25000 亿）量级
- [ ] **Step 6:** 提交 `feat: TushareSource 节流、重试与单位换算`

---

### Task 4: FredSource 与 Demo 数据源

**Files:**
- Create: `fred.py`, `demo.py`, `tests/test_fred.py`, `tests/test_demo.py`

**Interfaces:**
- Consumes: `TushareSource` 的公开方法签名（Task 3）
- Produces:
  - `class FredSource(fetch_text=None)`；`series(series_id: str, start: str) -> pd.Series | None`（索引 `pd.Timestamp`，值 float，`"."` → NaN；URL `https://fred.stlouisfed.org/graph/fredgraph.csv?id={id}&cosd={YYYY-MM-DD}`；首列无论叫 `DATE` 还是 `observation_date` 都当日期；任何异常或解析失败 → 记 WARNING 返回 None；超时 20 秒）
  - `class DemoSource(seed: int = 7)`：与 `TushareSource` 公开方法同名同签名（`call`/`from_env` 除外），数据随机但单位与量级合理；`fund_share` 深市代码最新一日故意留空以演示「待披露」
  - `class DemoFred(seed: int = 7)`：同 `FredSource.series`

- [ ] **Step 1: 写失败测试**

```python
def test_fred_parses_dot_as_nan():
    s = FredSource(lambda url: "observation_date,WALCL\n2026-10-28,6745635\n2026-11-04,.\n").series("WALCL", "2026-09-01")
    assert s.iloc[0] == 6745635 and math.isnan(s.iloc[1])
def test_fred_non_csv_returns_none():
    assert FredSource(lambda url: "<html>blocked</html>").series("WALCL", "2026-09-01") is None
def test_fred_exception_returns_none():
def test_demo_matches_tushare_interface():
    for name, fn in public_methods(TushareSource):   # 排除 call、from_env
        assert inspect.signature(getattr(DemoSource, name)) == inspect.signature(fn)
    assert inspect.signature(DemoFred.series) == inspect.signature(FredSource.series)
```

- [ ] **Step 2:** FAIL → **Step 3:** 实现 → **Step 4:** PASS
- [ ] **Step 5:** 提交 `feat: FRED 数据源与 demo 数据源`

---

### Task 5: metrics_etf（国家队）

**Files:**
- Create: `metrics_etf.py`, `tests/test_metrics_etf.py`

**Interfaces:**
- Produces（输入的 wide 表 = index 交易日 str，columns ETF 代码）：
  - `net_flow(share: DataFrame, close: DataFrame) -> DataFrame`（亿元 = 当日份额 − 前一交易日份额 × 当日收盘；任一端 NaN 则 NaN）
  - `aggregate(net: DataFrame, min_ratio: float = 0.8) -> pd.Series`（逐日 `sum(min_count=1)`；当日非空只数 / 列数 < min_ratio → NaN）
  - `round_summary(daily: pd.Series, start: str, attribution: tuple[float, float], count: int) -> dict`（键见 payload `round`；total 只加非 NaN；last3 = 最近 3 个交易日从新到旧，NaN → None）
  - `monthly_totals(daily: pd.Series, months: int) -> list[dict]`（label `"{m}月"`，days = 该月在序列中的交易日数，value = `sum(min_count=1)`）
  - `amount_groups(amount: DataFrame, today: str, yday: str, groups: dict[str, list[str]]) -> list[dict]`（chg_pct 取整；ratio = today/yday×100 取整；yday 为 0/NaN → chg_pct、ratio 为 None）
  - `tail_ratio(minutes: DataFrame | None, start: str = "14:30") -> float | None`（trade_time 的时分 > start 的 amount 之和 ÷ 全天之和 ×100）
  - `fingerprint_conclusion(broad_chg: float | None, industry_chg: float | None) -> str`
  - `ratio_footnote(groups: list[dict]) -> str`

- [ ] **Step 1: 写失败测试**

```python
def test_net_flow_keeps_nan():
    share = pd.DataFrame({"A": [10.0, 10.5, np.nan]}, index=["d1","d2","d3"])
    close = pd.DataFrame({"A": [4.0, 4.0, 4.0]}, index=["d1","d2","d3"])
    out = net_flow(share, close)
    assert np.isnan(out.A["d1"]) and out.A["d2"] == pytest.approx(2.0) and np.isnan(out.A["d3"])
def test_aggregate_disclosure_rule():   # 5 列：某日 3 只有值 → NaN；某日 4 只有值 → 4 只之和
def test_round_summary_attribution():
    r = round_summary(pd.Series([60.0, np.nan, 40.0], index=["20261103","20261104","20261105"]), "20261103", (0.7, 0.9), 29)
    assert (r["total"], r["low"], r["high"], r["mid"]) == (100.0, 70.0, 90.0, 80.0)
    assert r["last3"][1] == {"date": "20261104", "value": None}
def test_monthly_totals_labels():       # 10 月 2 天 + 11 月 1 天 → [{"label":"10月","days":2,...},{"label":"11月","days":1,...}]
def test_amount_groups_zero_yday():     # yday=0 → chg_pct None, ratio None
def test_tail_ratio():                  # 14:29 → 100，14:31 → 50，14:59 → 50 → 50.0
def test_conclusion_case1():
    assert fingerprint_conclusion(-25, 5) == "宽基通道明显降温、行业 ETF 反而 +5%——托底的钱在收手，进攻的钱在换标的。但成交降温 ≠ 净赎回，方向要等份额回填才能定。"
def test_conclusion_other_cases():      # (+15,−3) 含「资金向宽基集中」；(+12,+11) 含「整体活跃度上升」；(−12,−11) 含「整体降温」；(3,2) 含「变化不大」；(None,5) 返回含「数据不足」
def test_ratio_footnote():
    # 只有行业对照六只 105 → "只有行业对照六只越过了 100%（+5%），其余全部低于 100%"；全部 <100 → "四组全部低于 100%"
```

- [ ] **Step 2:** FAIL → **Step 3:** 实现（其余四个结论文案按 PROMPT §4.5 的意思写成一句完整中文）→ **Step 4:** PASS
- [ ] **Step 5:** 提交 `feat: ETF 净申赎与成交指纹计算`

---

### Task 6: metrics_sentiment（体温与韭菜浓度）

**Files:**
- Create: `metrics_sentiment.py`, `tests/test_metrics_sentiment.py`

**Interfaces:**
- Produces:
  - `limit_threshold(ts_code: str, name: str) -> float`（含 ST → 5；300/301/688/689 开头 → 20；否则 10）
  - `limit_flags(daily: DataFrame, names: dict[str,str]) -> DataFrame`（加 `up_limit`、`down_limit` 布尔列；判定 `pct_chg ≥ 阈值 − 0.2` / `≤ −(阈值 − 0.2)`；先剔除 `.BJ`）
  - `temperature_series(daily_by_date: dict[str, DataFrame], names: dict[str,str], ma: int = 20) -> DataFrame`（index 交易日，列 `up_ratio, median, limit, volume, profit, score`；量能历史 < 5 日的交易日整日不输出；量能均值用前 min(20, 可用) 日）
  - `temperature_kpis(series: DataFrame, window: int = 30) -> dict`（today, yday, delta, delta_text, mean, dev, dev_text, rank, n, pct_above, max, max_date；文案阈值照 PROMPT §4.1）
  - `match_sectors(basic: DataFrame, sw: DataFrame | None, recent_limit_codes: set[str]) -> tuple[dict[str, set[str]], str]`（返回板块→代码集合，以及口径名 `"申万行业"` 或 `"stock_basic.industry"`；sw 名称先去掉「Ⅱ」「Ⅲ」再子串匹配任一层）
  - `concentration(mf: DataFrame, codes: set[str] | None = None) -> float | None`（金额加权：Σ(买+卖)(sm+md) ÷ Σ(买+卖)(四档) ×100；无成分 → None）
  - `six_dim(five_score: float | None, market_conc: float | None) -> float | None`（= (5×five + conc) ÷ 6；任一 None → None）
  - `concentration_wall(mf_today, mf_yday, sectors_today, sectors_yday) -> list[dict]`（按 today 降序；color 按名次三等分：`i < n/3` red，`i < 2n/3` orange，否则 blue；yday 为 None → chg None，前端显示「新增」；today 为 None 的板块排最后、color gray）

- [ ] **Step 1: 写失败测试**

```python
def test_limit_threshold():
    assert limit_threshold("300750.SZ", "宁德时代") == 20
    assert limit_threshold("600001.SH", "*ST某某") == 5
    assert limit_threshold("688981.SH", "中芯国际") == 20
    assert limit_threshold("000001.SZ", "平安银行") == 10
def test_temperature_dims():
    # 构造 6 个交易日（前 5 天为历史，量能均额 100 亿），第 6 天：4 只股票 pct [2.0, 2.0, 10.0, -1.0]，总额 110 亿，
    # 前一日涨停 2 只、其中 1 只今日上涨
    row = temperature_series(...).iloc[-1]
    assert row.up_ratio == 75 and row.median == 75 and row.limit == 100 and row.volume == pytest.approx(60) and row.profit == 50
def test_limit_dim_both_zero_is_50():
def test_volume_needs_5_days():         # 只有 4 天历史的那一天不在输出 index 中
def test_concentration_is_amount_weighted():
    # A: sm+md 60 / 总 100；B: 10 / 总 300 → 17.5（简单平均会是 31.67）
    assert concentration(mf) == pytest.approx(17.5)
def test_match_sectors_strips_roman():   # sw l2_name "白酒Ⅱ" → 归入「消费白酒·零售」；sw=None → 口径 "stock_basic.industry"
def test_wall_colors_and_new():          # 6 行 → 2 red / 2 orange / 2 blue；yday None → chg None
def test_six_dim():
    assert six_dim(71.2, 65.2) == pytest.approx(70.2) and six_dim(None, 65.0) is None
```

- [ ] **Step 2:** FAIL → **Step 3:** 实现 → **Step 4:** PASS
- [ ] **Step 5:** 提交 `feat: 市场体温与韭菜浓度计算`

---

### Task 7: metrics_liquidity（流动性、外围、板块流向、新闻）

**Files:**
- Create: `metrics_liquidity.py`, `tests/test_metrics_liquidity.py`

**Interfaces:**
- Produces:
  - `make_card(key, label, series: pd.Series, unit, *, change="diff"|"pct", spark_n=30, note="", tone=None) -> dict`（取最后一个非 NaN 值及其日期；change 相对前一个非 NaN；series 全空 → value None、note「无数据」）
  - `liquidity_vote(turnover_ratio: float|None, margin_chg_pct: float|None, flow_5d: float|None, shibor_diff_bp: float|None, cfg: dict = config.VOTE) -> dict`（返回 `{score, label, signals:[{name, dir:+1|0|-1|None, detail}]}`；None 信号 dir=None 不计票）
  - `fed_net_liquidity(walcl: pd.Series, tga: pd.Series, rrp: pd.Series) -> pd.Series`（十亿美元 = walcl/1000 − tga/1000 − rrp；rrp 用 `asof` 对齐到 walcl 日期）
  - `rate_expectation(dff: float|None, fed30d: float|None, band_bp: float = 10) -> dict`（implied = 100 − fed30d；diff_bp = (implied − dff)×100；label「市场预期降息 / 预期加息 / 预期按兵不动」；任一 None → label「无数据」）
  - `sector_flow_rank(df: DataFrame, end: str, days: int, top: int = 10) -> dict`（取 ≤ end 的最近 days 个交易日，按 industry 汇总 net_amount；pct 取 end 当日；返回 `{inflow:[...降序], outflow:[...升序]}`，每行 `{name, net, pct}`）
  - `filter_news(df: DataFrame, kw_domestic: list[str], kw_external: list[str]) -> dict`（两组都命中归 domestic；按 pub_time 倒序；同标题去重；`flag = 标题含(互换便利|再贷款) 且匹配 r"\d+(\.\d+)?\s*(亿|万亿)"`）
  - `load_policy_ops(path: str) -> list[dict]`（CSV 列 `date,tool,amount_yi,cum_yi,url,note`；空值 → None；文件只有表头 → []）

- [ ] **Step 1: 写失败测试**

```python
def test_vote_loose():
    v = liquidity_vote(1.15, 0.6, -10.0, -6.0)
    assert v["score"] == 2 and v["label"] == "偏松" and [s["dir"] for s in v["signals"]] == [1, 1, -1, 1]
def test_vote_missing_signal_not_counted():
    v = liquidity_vote(None, -0.6, -1.0, 0.0)
    assert v["signals"][0]["dir"] is None and v["score"] == -2 and v["label"] == "偏紧"
def test_fed_net_liquidity():
    s = fed_net_liquidity(ser({"2026-11-04": 6745635}), ser({"2026-11-04": 957175}), ser({"2026-11-04": 0.125}))
    assert s.iloc[-1] == pytest.approx(5788.335)
def test_rate_expectation():
    assert rate_expectation(3.88, 96.113)["label"] == "预期按兵不动"
    r = rate_expectation(3.88, 96.30); assert r["diff_bp"] == pytest.approx(-18) and r["label"] == "市场预期降息"
    assert rate_expectation(None, 96.1)["label"] == "无数据"
def test_sector_flow_rank_5d():          # 两个行业 5 天累计，inflow[0] 为累计最大者
def test_filter_news():
    # 输入含：重复标题两条；「华福证券中标央行第三批互换便利操作12亿元」；「英国央行三年多来首度加息」；「央行与美联储」双命中
    assert d["domestic"][0]["flag"] is True                  # 12亿元那条
    assert any("英国央行" in n["title"] for n in d["external"])
    assert not any("美联储" in n["title"] and "央行与" in n["title"] for n in d["external"])
def test_make_card_empty():              # 全 NaN → value None, note "无数据"
def test_load_policy_ops_header_only(tmp_path):
```

- [ ] **Step 2:** FAIL → **Step 3:** 实现 → **Step 4:** PASS
- [ ] **Step 5:** 提交 `feat: 流动性、外围、板块流向与新闻计算`

---

### Task 8: summary、render、build（端到端 demo）

**Files:**
- Create: `summary.py`, `render.py`, `build.py`, `template.html`（本任务只放最小骨架：`<script>const DATA = /*__DATA__*/null;</script>` + 横幅位置），`data/policy_ops.csv`（只有表头），`tests/test_summary.py`, `tests/test_render.py`, `tests/test_build.py`

**Interfaces:**
- Consumes: Task 3–7 全部 Produces
- Produces:
  - `summary.national_team_verdict(last3: list[float]) -> tuple[str, str]`（入参为最近 3 个**已披露**日的值，新→旧；返回 (label, tone)；规则见 SPEC §4.0）
  - `summary.build_conclusions(vote: dict, rate_expect: dict, fed_card: dict, sector_day: dict, team_last3: list[float], team_asof: str, round_total: float | None, temp_kpis: dict, market_conc: float | None, market_conc_yday: float | None) -> list[dict]`（四张卡，文案格式照 SPEC §4.0 表；`team_last3` 是 SH11 合计序列中最近 3 个非 NaN 值，新→旧，`team_asof` 为其中最新一个的日期；`fed_card` 是外围「美联储净流动性」卡，change 字段为 4 周变化）
  - `render.to_json(payload: dict) -> str`（`allow_nan` 下先把 NaN/inf 递归换成 None；numpy 标量转 Python；`</` → `<\/`）
  - `render.render(template_path: str, payload: dict, out_path: str) -> None`（替换唯一一处 `/*__DATA__*/null`，找不到则抛错）
  - `build.resolve_date(arg: str | None, now: datetime | None = None) -> str`（None → `Asia/Shanghai` 当日 YYYYMMDD）
  - `build.collect(src, fred, date: str, round_start: str | None, demo: bool) -> dict`（返回 payload 契约全部键）
  - `build.main(argv: list[str] | None = None) -> int`（参数 `--date --out(默认 site/index.html) --round-start --demo`；建 out 目录；结束时 INFO 打印 warnings 汇总）
- 交易日判定（写进 `collect`）：`D` = ≤ date 的最近开市日中 `stock_daily` 非空的最新一个（最多回看 3 个开市日）。moneyflow、sector_flow、etf_minutes、market_flow 各自从 D 往前找最近非空日（最多 3 个开市日），面板记录自己的 `date`；与 D 不同则 WARNING「X 数据截至 M/D」

- [ ] **Step 1: 写失败测试**

```python
def test_team_verdict():
    assert national_team_verdict([100, -20, 50])[0].startswith("仍在")
    assert national_team_verdict([-100, -20, 50])[0].startswith("疑似撤出")
    assert national_team_verdict([100, -150, 20])[0] == "放缓"
def test_render_escapes_script_close():
    s = to_json({"t": "</script><b>x", "n": float("nan")})
    assert "</script>" not in s and json.loads(s) == {"t": "</script><b>x", "n": None}
def test_render_requires_placeholder(tmp_path):
def test_resolve_date_uses_shanghai():
    assert resolve_date(None, datetime(2026, 11, 6, 17, 0, tzinfo=timezone.utc)) == "20261107"
def test_build_demo_end_to_end(tmp_path):
    assert main(["--demo", "--out", str(tmp_path/"i.html"), "--date", "20261106"]) == 0
    html = (tmp_path/"i.html").read_text()
    assert "/*__DATA__*/null" not in html
    data = json.loads(extract_data_json(html))
    assert data["meta"]["demo"] is True and len(data["conclusions"]) == 4
    assert set(data) >= {"meta","conclusions","indices","liquidity","policy_ops","news","sector_flow",
                         "etf_flow","round","monthly","fingerprint","temperature","concentration"}
def test_build_when_moneyflow_not_ready(tmp_path):   # DemoSource 子类：moneyflow(D) 与 sector_flow 当日返回空
    assert data["concentration"]["date"] != data["meta"]["trade_date"]
    assert any("数据截至" in w for w in data["meta"]["warnings"])
def test_build_skips_missing_etf(tmp_path):          # DemoSource 子类：etf_share("159919.SZ", ...) 返回空
    assert data["round"]["count"] == 28 and any("159919.SZ" in w for w in data["meta"]["warnings"])
def test_build_fred_down(tmp_path):                  # DemoFred 子类 series 返回 None → 外围卡 value None，构建成功
```

- [ ] **Step 2:** FAIL → **Step 3:** 实现。取数窗口：daily 取 `TEMP_WINDOW + AMOUNT_MA + 2` 个开市日；ETF 份额与日线取 `max(FLOW_MONTHS, MONTHLY_MONTHS)` 个月 + 5 个开市日；资金流向图用最近 `FLOW_MONTHS` 个月；卡片 spark 取 30 个开市日；FRED 取近 400 天；新闻取 `NEWS_DAYS` 个自然日。ETF 代码全集 = `FIXED_BROAD ∪ BROAD10 ∪ INDUSTRY6`，每只单独取，失败或空 → WARNING 跳过。`round.start` = `--round-start` 或 `config.ROUND_START` 或 D 往前 38 个自然日后的第一个开市日。尾盘占比：CORE5 五只当日（及前一日）分钟线 concat 后传给 `tail_ratio`，任一只缺失则该日为 None 且 `tail.source = "无分钟数据"`，否则 `"stk_mins"`
- [ ] **Step 4:** PASS（`pytest -q` 全绿）
- [ ] **Step 5:** 提交 `feat: 结论、渲染与构建入口`

---

### Task 9: 页面上半部（页头、结论条、流动性、板块流向）

**Files:**
- Modify: `template.html`

**Interfaces:**
- Consumes: payload 契约的 `meta, conclusions, indices, liquidity, policy_ops, news, sector_flow`

- [ ] **Step 1:** 实现，按 SPEC §4.0、§4.1、§4.2 与 PROMPT §5：
  - 页头一行元信息；`meta.demo` 时顶部红色横幅「当前为随机模拟数据」；`meta.warnings` 非空时页脚折叠列表
  - 结论条 4 卡（桌面 4 列，≤900px 2 列，≤560px 1 列），tone → 左边框色
  - 流动性：大盘涨跌条；「国内」卡片网格（每卡 value + change + ECharts 迷你线，或无数据灰字）；投票明细（四个信号逐项显示方向箭头与 detail）；「外围」卡片网格 + 加减息预期一行；政策工具表（空时显示「待手工补充 · data/policy_ops.csv」）；新闻两页签（flag 条目带橙色标签「疑似新操作 · 请核对」）
  - 板块流向：横向发散条形图，「当日 / 近 5 日累计」切换
  - 每节标题前 4px 红竖条；数字 `font-variant-numeric: tabular-nums`
- [ ] **Step 2:** `python build.py --demo --out site/index.html`，用浏览器面板打开 `site/index.html`：控制台无报错；在 1300px 与 390px 各截图；390px 下 `document.documentElement.scrollWidth <= 390`；两种宽度下四句结论条首屏可见（390px 下允许在首屏 812px 高度内）
- [ ] **Step 3:** 临时把一条 demo 新闻标题改成 `<img src=x onerror=alert(1)>测试`，确认页面显示为文字且无弹窗，然后还原
- [ ] **Step 4:** 提交 `feat: 页面上半部`

---

### Task 10: 页面下半部（国家队、散户情绪）

**Files:**
- Modify: `template.html`

**Interfaces:**
- Consumes: payload 的 `etf_flow, round, monthly, fingerprint, temperature, concentration`

- [ ] **Step 1:** 实现，逐条照 PROMPT §4.3、§4.4、§4.5（国家队）与 §4.1、§4.2（散户情绪）的展示要求，参照用户参考截图的版式：资金流向三段切换（默认单日）；底部两栏（本轮净申赎卡片 + 月度柱 | 成交指纹列表 + 结论框 + 比值条 + 底注）；体温 4 KPI + 色带图例 + 折线（最高红点、最低绿点、今日大号红字与浅橙底高亮）+ 分项说明；浓度墙 HTML 表（≤560px 隐藏浓度条列，黑竖刻度 = 昨日值）。全部图表 `window.resize` 时 `resize()`
- [ ] **Step 2:** 同 Task 9 Step 2 的 demo 构建与双宽度截图检查；确认 demo 的深市最新日在本轮卡片上显示「待披露」而非 0
- [ ] **Step 3:** 提交 `feat: 页面下半部`

---

### Task 11: 真实数据运行、抽查、政策表初始数据、README

**Files:**
- Modify: `data/policy_ops.csv`
- Create: `README.md`

- [ ] **Step 1:** `python build.py --out site/index.html 2>&1 | tee /tmp/build.log`（真实 token）→ 退出码 0；汇总 WARNING
- [ ] **Step 2:** 抽查 1（PROMPT 验收 3）：任选 1 只 ETF 1 个交易日，用 `fund_share` 份额差 × `fund_daily` 收盘手算，与页面 `etf_flow.per_etf` 推出的净申赎一致（误差 < 0.01 亿）
- [ ] **Step 3:** 抽查 2（验收 4）：任选 1 个板块，用 moneyflow 手算浓度，与 `concentration.rows` 一致（误差 < 0.01pp）
- [ ] **Step 4:** 抽查 3（验收 5）：若深市最新日份额未更新，确认页面为「待披露」
- [ ] **Step 5:** 抽查 4：美联储净流动性用 FRED 网页上的三个值手算一致
- [ ] **Step 6:** 用 `major_news` 按「互换便利|再贷款」检索近 60 天，整理出能确认日期、工具、金额的操作写入 `data/policy_ops.csv`，每行带新闻 url；不能确认金额的不写；一行都确认不了就保留空表
- [ ] **Step 7:** README：运行方法；每个面板口径（写明「韭菜浓度」「体温」「流动性投票」是近似 / 自定口径，不声称与任何视频算法一致；10 年国债期货是收益率的反向代理；归因比例是假设）；用到的 tushare 接口清单与权限说明；FRED 数据说明；`policy_ops.csv` 维护方法
- [ ] **Step 8:** 浏览器打开真实数据页面，1300px 与 390px 各截一张全页图
- [ ] **Step 9:** 提交 `feat: 真实数据校验、政策表与说明文档`

---

### Task 12: 定时构建与 Pages 发布

**Files:**
- Create: `.github/workflows/build.yml`
- Delete: `.github/workflows/smoke.yml`

**Interfaces:**
- Consumes: Task 2 的冒烟结论。若 FAIL，本任务改为：`scripts/run_local.sh`（`build.py` → 把 `site/` 推到 `gh-pages` 分支）+ `~/Library/LaunchAgents/com.qqjjtt.market-dashboard.plist` 三个 `StartCalendarInterval`（周一至周五 16:30、20:30、09:30），并请用户把 Pages Source 改为 `gh-pages` 分支

- [ ] **Step 1:** 写 `build.yml`：
  - `on.schedule`: `cron: "30 8 * * 1-5"`、`cron: "30 12 * * 1-5"`、`cron: "30 1 * * 1-5"`；另加 `workflow_dispatch`
  - `permissions: {contents: read, pages: write, id-token: write}`；`concurrency: {group: pages, cancel-in-progress: false}`
  - job build：checkout → setup-python 3.12 → `pip install -r requirements.txt` → `pytest -q` → `python build.py --out site/index.html`（env `TUSHARE_TOKEN: ${{ secrets.TUSHARE_TOKEN }}`）→ `actions/configure-pages@v5` → `actions/upload-pages-artifact@v3`（path `site`）
  - job deploy：`needs: build`，`environment: github-pages`，`actions/deploy-pages@v4`
  - 构建失败则不部署，线上保留上一版
- [ ] **Step 2:** 提交并 `git push`
- [ ] **Step 3:** 请用户在 Actions 页手动 Run workflow 一次；成功后打开 `https://qqjjtt.github.io/market-dashboard/`，确认外网可访问、页头生成时间为北京时间
- [ ] **Step 4:** 最终报告给用户（PROMPT 验收 7）：成功的面板；降级的面板及原因；取不到数据的 ETF 代码；实际用到的接口及所需权限；日志告警汇总；两张截图

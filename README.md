# 分片键评估与倾斜检测（从 0 实现）

## 范围

交付一个离线评估器：`shardeye` 包（库 + `python -m shardeye` 命令行入口），外加 `web/` 下用原生 HTML/Canvas 写的分片负载页面。用 Python 3.13 标准库，不引第三方依赖、不加构建步骤，测试写在 unittest 里。起始目录里没有代码，库、页面、测试都要新写。

只做三件事：读一份 JSONL 数据，按给定候选键统计分片分布并算出指标；判定有没有倾斜；按固定规则给出改进建议。另外把同一份结果写成页面数据。

明确不做：不连数据库、不搬数据、不做在线重分片；不解析 JSONL 以外的格式；不做并发与多进程；不评估查询性能；页面只负责展示，不重算任何数字。

## 口径与公式

- 数据：UTF-8 的 JSONL，一行一个对象；字段值只允许字符串或 null，空行非法。
- 候选键：`--keys` 里用逗号分隔；组合键写 `a+b`，按字段顺序取值后用 `|` 拼成一串。字段只在 `+` 处切一层，不支持嵌套路径。
- 规范化：字段缺失、null、空字符串一律记为空值 `∅`（U+2205），参与统计和哈希。
- 分片：`--shards k`（样例都是 16）；`分片号 = FNV-1a64(数据串的 UTF-8 字节) % k`。FNV-1a64 取 `h = 14695981039346656037`，逐字节 `h = ((h ^ b) * 1099511628211) mod 2^64`。不要用内置 `hash()`，它带随机盐，两次运行结果会不一样。
- 抽样：上限 `S = 20000` 行。总行数 `N ≤ S` 时全量；否则步长 `L = ceil(N / S)`，取行号满足 `i mod L == 0` 的行，再固定补上最后一行 `N-1`（行号从 0 起，样本最多 20001 行）。行数要先数一遍，允许顺序读两遍文件。
- 指标（都用样本计算；`m` 是样本行数，`c[i]` 是分片 i 的样本计数）：
  - 基数 `distinct`：样本里不同数据串的个数，空值算一个取值。
  - `max_min_ratio = max(c) / min(c)`，保留两位小数；`min(c) = 0` 时记作 `inf`。
  - `stdev`：总体标准差 `sqrt(Σ(c[i] - m/k)² / k)`，保留两位小数（`round(x, 2)`）。
- 倾斜判定（阈值写死）：`distinct < k`，或者 `2 * k * max(c) >= 3 * m`（即最大分片达到平均负荷的 1.5 倍）。
- 排序：`max_min_ratio` 升序（`inf` 排最后）→ `stdev` 升序 → `distinct` 降序 → 命令行里候选键的先后。输出行序就是排序结果，页面行序也用它。
- 建议（每个键按下面的顺序命中即停）：
  1. `distinct < k` → `replace:<X>`：X 取排序里第一个不是本键的候选键，没有就写 `-`。
  2. 存在取值满足 `count / m >= 1/5` → `salt:<值>:<s>`：取占比最高的取值，并列时取 UTF-8 字节序最小的那个；盐个数 `s = ceil(count * k / m)`。加盐指哈希前把该值拼成 `值#0` … `值#(s-1)`。
  3. 倾斜但没有占比 ≥ 1/5 的取值 → `compose:<Y>`：Y 取其它候选中 `distinct` 最大者，并列按命令行顺序，没有就写 `-`。
  4. 其余 → `keep`。

## 数据结构

- 候选键就是字段名或 `+` 连接的多字段，值在统计前统一规范化成数据串。
- 每行只解析一次，所有候选键共用同一份样本；每个键维护 `k` 个分片计数和一张「取值 → 样本计数」表。
- 内存与样本同阶（≤ 20001 行加每键一张表），不缓存任何原始行。

## 输入输出与文件格式

命令：

```
python -m shardeye --data samples/data/01-uniform.jsonl --keys order_id,user_id --shards 16
```

stdout 用制表符分隔、UTF-8、LF，行序就是排序结果：

```
SAMPLE	<样本行数>	<总行数>	<分片数>
FIELDS	key	distinct	max_min_ratio	stdev	skewed	suggestion	counts
KEY	<键名>	<整数>	<两位小数或 inf>	<两位小数>	<yes/no>	<建议>	<c0,c1,…,c15>
```

- 建议写成 `keep`、`salt:值:s`、`compose:目标` 或 `replace:目标`；空值写成 `∅`，没有目标写 `-`。
- 退出码：0 正常；空行、非法 JSON、非字符串/null 的值都算输入错误，stderr 打一行 `bad-row <行号>`，退出码 2。
- 每次运行都写 `<web-dir>/data.json`（`--web-dir` 默认 `web`，目录不存在就创建）：

```json
{"shards":16,"rows_total":60000,"rows_sampled":20001,
 "candidates":[{"key":"sku","distinct":1201,"max_min_ratio":8.48,"stdev":1447.59,
   "skewed":true,"counts":[…16 个整数…],"overloaded":[…16 个布尔…],
   "suggestion":{"type":"salt","value":"sku-HOT","salt_count":5}}]}
```

  `overloaded[i]` 表示 `2 * k * c[i] >= 3 * m`；`max_min_ratio` 为 `inf` 时 JSON 里写 `null`；`suggestion.type` 取 `keep/salt/compose/replace`，没有目标时 `target` 为 `null`。
- 页面 `web/index.html` 用原生 ES module + Canvas：在 `web/` 下跑 `python -m http.server 8000` 再打开 `http://localhost:8000/`（`file://` 下 fetch 会被浏览器拦掉）。每个候选键一行：Canvas 画 k 根柱，柱高为该分片计数，`overloaded` 的柱标红；行内显示键名、三项指标（`max_min_ratio`、`stdev`、`distinct`）、`skewed` 和建议；顶部显示 `rows_total`、`rows_sampled`、`shards`。所有数字只能取自 `data.json`。

## 性能与验收口径

- 预算：常驻内存不随 `N` 增长；3200 万行、3 个候选键、16 分片时单次运行峰值 ≤ 128 MB、耗时 ≤ 120 秒（单核顺序读）；样例文件最大 6 万行，单次 ≤ 2 秒。
- 验收：
  1. 样例命令的 stdout 与 `samples/expected/` 对应文件逐字节一致；
  2. 同一份数据连跑两次，stdout 完全一致（不得用内置 `hash()`，不得有无种子的随机）；
  3. 页面柱高与指标和 `data.json`、stdout 一致，页面自己不重算；
  4. 只用标准库、没有构建步骤，`python -m shardeye` 直接能跑；
  5. 抽样的公式必须照上面实现：06 号样例的热点全在文件尾部，漏掉它就会判错。

## 样例说明

现场背景见 `samples/notes.md`。每组数据的期望结果都是同一份数据跑出来的完整 stdout，逐行可核对；跑样例时建议加 `--web-dir var/web`，免得往交付目录里写页面数据。

- `01-uniform.jsonl` ↔ `expected/01-uniform.txt`：天然均匀键（`--keys order_id,user_id`）→ 两键都 `keep`。
- `02-hot-tenant.jsonl` ↔ `expected/02-hot-tenant.txt`：一个租户占 65%（`--keys tenant_id,order_id`）→ `salt:vip-0001:11`。
- `03-correlated.jsonl` ↔ `expected/03-correlated.txt`：省份与城市高度相关（`--keys province,city,province+city`）→ `city` 与组合键都是「单值 35%」的 `salt:…:6`，组合键的 `distinct` 和 `city` 相同，说明相关性没有带来新的散列空间；`province` 只有 4 个取值，判 `replace:city`。
- `04-nulls.jsonl` ↔ `expected/04-nulls.txt`：空值占 70%（`--keys channel,order_id`）→ `salt:∅:12`。
- `05-low-cardinality.jsonl` ↔ `expected/05-low-cardinality.txt`：只有 3 个取值（`--keys status,order_id`）→ `replace:order_id`。
- `06-tail-hot.jsonl` ↔ `expected/06-tail-hot.txt`：热点全在文件尾部，60000 行触发抽样（样本 20001 行）（`--keys sku,warehouse`）→ 抽样后仍判倾斜，`salt:sku-HOT:5`。
- `07-bumpy.jsonl` ↔ `expected/07-bumpy.txt`：没有单值热点、但中等频次取值扎堆（`--keys promo,user_id`）→ `compose:user_id`。

命令模板：`python -m shardeye --data samples/data/<文件> --keys <键> --shards 16`，期望结果在 `samples/expected/<同名前缀>.txt`。把 stdout 存成文件后跟它逐字节比较即可。

## 待补的文档

- 实现完成后补：命令行参数与退出码说明、3200 万行的实测内存与耗时、抽样取舍与已知限制（比如样本内出现空分片时 `max_min_ratio` 记 `inf`）。
- 本目录只有这份说明和 `samples/` 素材，库、页面与测试都要新写。

## 使用说明（实现完成后补）

### 目录

- `shardeye/`：评估库与 `python -m shardeye` 入口（纯标准库）。
- `web/index.html`：原生 ES module + Canvas 的负载页面；`web/data.json` 由每次运行写出。
- `tests/test_shardeye.py`：unittest 测试，含 7 组样例的逐字节回归。
- `var/`：本地运行产物（临时输出、基准数据），不交付。

### 命令行参数

```
python -m shardeye --data <文件.jsonl> --keys <候选键> --shards <k> [--web-dir <目录>]
```

- `--data`：UTF-8 JSONL，一行一个对象（必填）。
- `--keys`：候选键，逗号分隔；组合键用 `+` 连接并按字段顺序取值，如 `province+city`（必填）。
- `--shards`：分片数 k，正整数（必填）。
- `--web-dir`：`data.json` 的输出目录，默认 `web`，不存在则创建。
- stdout：制表符分隔的评估结果（见前文「输入输出与文件格式」），UTF-8、LF。

### 退出码

- `0`：正常。
- `2`：输入错误（空行、非法 JSON、非对象行、字段值不是字符串或 null），stderr 打一行 `bad-row <行号>`（行号从 0 起）；也用于参数错误（如 `--shards` 非正、候选键为空）。

### 查看页面

```
python -m shardeye --data samples/data/06-tail-hot.jsonl --keys sku,warehouse --shards 16
(cd web && python -m http.server 8000)
# 浏览器打开 http://localhost:8000/ ；不要用 file://，fetch 会被浏览器拦截
```

### 3200 万行实测

合成数据 32,000,000 行、每行约 63 字节（约 2.0 GB），3 个候选键、16 分片，单核顺序读：

| 指标 | 实测 | 预算 |
| --- | --- | --- |
| 耗时 | 24.5 s | ≤ 120 s |
| 峰值常驻内存（max RSS） | 17,372 KB（约 17 MB） | ≤ 128 MB |

环境：Intel Core Ultra 7 265K 单核、Python 3.14。内存只随样本量（≤ 20001 行）和每个候选键的取值表增长，与总行数无关。

### 抽样取舍与已知限制

- 全量扫几千万行太慢，而只抽开头会躲过尾部热点（06 号样例）。口径是步长抽样（`i mod L == 0`）并固定补最后一行，样本最多 20001 行；文件被顺序读两遍（数行 + 统计），非样本行只做合法性校验、不建任何对象。
- 结论只代表样本：极端罕见、且恰好全部落在非采样行的热点仍可能被漏掉，但按固定步长 + 尾行，尾部热点不会被系统性躲过。
- 样本里出现空分片时，`max_min_ratio` 记为 `inf`（stdout）/ `null`（JSON），排序时排在最后。
- 指标基于样本计数而非外推估算，`stdev` 用总体标准差；`distinct` 是样本内基数，全量基数可能更高。
- 字段只按顶层名取值，`+`/`,` 只切一层，不支持嵌套路径；组合键分隔符是字面量 `|`，取值本身含 `|` 时不做转义（按字面拼接即可，同一候选键下不产生歧义）。
- 建议只按 README 的固定规则生成，不评估查询性能，不保证加盐/换键后的真实收益。

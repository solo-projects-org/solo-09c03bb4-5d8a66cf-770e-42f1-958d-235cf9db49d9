# 发票冲红补样 API

数仓质检补样服务：发票抽查单已下发，对冲红（红冲作废）的票号按批退出样本/备选名单，
并从共享备选池中为各核查专题补样。FastAPI + SQLite，Docker Compose 一键启动，Swagger 可直接操作。

## 功能

- **按轮登记清册**：各核查专题的名额、已下发样本票号、备选票号；备选票可跨专题共享。
- **按批号提交冲红**：本批票号整批退出样本名单与备选名单；仍有效的已发样本照旧。
- **自动补样**：每张票最多占一个名额；用最大二部匹配保证**本批补上的名额总数最大**；
  回执给出各专题的替补票号与剩余缺额。
- **幂等**：同轮同批号重送同一组票号（与顺序无关）返回原回执（HTTP 200）；换一组票号报 409 冲突。
- **并发一致**：并发冲红在 `BEGIN IMMEDIATE` 写事务中串行生效，结果与按生效顺序（seq）逐批处理一致；
  任一步失败整批回滚。
- **持久化**：SQLite 落盘（Docker volume），重启后仍可查现行名单与回执。
- **回执核查（只读）**：按轮次+批号核对当时的备选票还能不能多补；给出备选不足的专题组、
  组内各专题补样前缺额与当时可用的全部备选票号（跨专题共享只列一次），并核验
  “组内缺额合计 − 备选票数 = 回执全轮剩余缺额”。以历史回执重放，后续冲红不改变结果。

## 快速开始

```bash
docker compose up --build
# 打开 http://localhost:8000/docs （Swagger UI）
```

本地运行（Python 3.11+）：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt   # 运行只需 requirements.txt
DB_PATH=./data/app.db .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 在 Swagger 里演示“共享备选不足的批次怎么核查”

`docker compose up --build` 后打开 <http://localhost:8000/docs>，依次操作：

1. `POST /rounds` → Try it out，填：

   ```json
   {"round_id": "DEMO-AUDIT", "topics": [
     {"topic_code": "价税异常",   "quota": 3, "samples": ["A1","A2","A3"], "candidates": ["C1","C2","C3"]},
     {"topic_code": "进销项背离", "quota": 3, "samples": ["B1","B2","B3"], "candidates": ["C2","C3","C4"]},
     {"topic_code": "顶额开具",   "quota": 2, "samples": ["D1","D2"],      "candidates": ["C1","C4"]}
   ]}
   ```

   （3 个专题共 8 个名额，备选只有 4 张且在专题间共享。）

2. `POST /rounds/DEMO-AUDIT/redflush-batches`，填
   `{"batch_no": "P1", "invoice_nos": ["A1","A2","A3","B1","B2","B3","D1","D2"]}`
   → 8 张样本全退，回执只能补 4 个，`shortfall` 合计 4。

3. `GET /rounds/DEMO-AUDIT/redflush-batches/P1/audit` → Execute。
   返回 `status=shortfall`，`topic_group.topics` 列出三个专题及各自补前缺额，
   `candidates` 为 `["C1","C2","C3","C4"]`（共享票只列一次），
   `check` 显示 `8 − 4 = 4`、`round_shortfall: 4`、`matches: true`。

4. 再提交 `POST /rounds/DEMO-AUDIT/redflush-batches`，
   `{"batch_no": "P2", "invoice_nos": ["C1","C2","C3","C4"]}`，然后重复第 3 步：
   同一张 P1 回执的核查结果与之前完全相同（后续冲红不影响历史核查）。

同样的流程也可以脚本一次跑完：

```bash
.venv/bin/python scripts/audit_demo.py http://127.0.0.1:8000
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/rounds` | 登记一轮清册（同内容重登幂等返回 200，不同内容 409） |
| GET | `/rounds/{round_id}` | 查现行名单：各专题名额/有效样本/可用备选/缺额 |
| POST | `/rounds/{round_id}/redflush-batches` | 按批号提交冲红票号，返回补样回执 |
| GET | `/rounds/{round_id}/redflush-batches` | 列出本轮全部回执（按生效顺序） |
| GET | `/rounds/{round_id}/redflush-batches/{batch_no}` | 查单个回执 |
| GET | `/rounds/{round_id}/redflush-batches/{batch_no}/audit` | **只读核查回执**：当时备选还能不能多补 |
| GET | `/health` | 健康检查 |

### 示例

```bash
# 登记清册：备选 C1~C4 跨专题共享
curl -X POST http://localhost:8000/rounds -H 'Content-Type: application/json' -d '{
  "round_id": "R2026-01",
  "topics": [
    {"topic_code": "价税异常",   "quota": 2, "samples": ["A1","A2"], "candidates": ["C1","C2","C3"]},
    {"topic_code": "进销项背离", "quota": 2, "samples": ["B1","B2"], "candidates": ["C2","C3","C4"]},
    {"topic_code": "顶额开具",   "quota": 1, "samples": ["D1"],      "candidates": ["C1","C4"]}
  ]}'

# 提交冲红批 P1
curl -X POST http://localhost:8000/rounds/R2026-01/redflush-batches \
  -H 'Content-Type: application/json' -d '{"batch_no": "P1", "invoice_nos": ["A1","B1"]}'

# 查现行名单与回执
curl http://localhost:8000/rounds/R2026-01
curl http://localhost:8000/rounds/R2026-01/redflush-batches/P1
```

回执示例（节选）：`topics[]` 含各专题 `vacancy_before`（补前缺额）、`replacements`（替补票号）、
`shortfall`（剩余缺额）；`removed` 记录本批退出名单的票号；`unknown_invoices` 为清册外票号。

## 回执核查（只读）

```bash
curl http://localhost:8000/rounds/R2026-01/redflush-batches/P1/audit
```

口径：**本批剔除冲红票之后、分配替补之前**的备选池；共享备选只算一张。
回执无缺额时返回 `status=fully_filled`、`message=已补满`、`topic_group=null`。
有缺额时返回：

| 字段 | 含义 |
| --- | --- |
| `topic_group.topics[]` | 备选不足的专题组，各专题 `vacancy_before`（补样前缺额） |
| `topic_group.vacancy_before_total` | 组内缺额合计 |
| `topic_group.candidates` | 该组当时可用的全部备选票号，跨专题共享只列一次 |
| `topic_group.candidate_count` | 备选票数（去重） |
| `topic_group.shortfall` | 缺额合计 − 备选票数（该组补不上的名额） |
| `check` | 与回执 `round_shortfall` 的核验：`matches=true` 表示等式成立 |

**专题组怎么来的**：把“缺额”当成左侧槽位、备选票当成右侧点的二部图，先求最大匹配；
从未匹配槽位沿交替路径做可达标记，由最大匹配性质（未匹配槽位不存在增广路）
可得“可达缺额数 − 可达备选数 = 未匹配槽位数”。把可达槽位所属专题作为专题组，
即得 `组内缺额合计 − 备选并集 = 该组剩余缺额`。若全轮还有别的专题无法补齐，
等式右端即回执的全轮剩余缺额。缺额组由拓扑唯一确定，与具体匹配方案无关。

**历史不变性**：核查只读 `rounds/topics/candidates/batches` 四张表，且只回放 `seq` 不晚于
本批的历史回执来重建备选池，不读现行 `samples/candidates` 状态。所以升级前已生效的批次
可以直接查，之后再有冲红也不会改变核查结果。

## 关键设计

- **数据模型**（SQLite）：`rounds`（清册摘要做幂等）、`topics`（名额+登记顺序）、
  `samples`（active 标记现行有效）、`candidates`（available 标记可用，同一票号可出现在多个专题）、
  `batches`（批号主键 + 生效顺序 seq + 规范化票号集合 + 回执原文）。
- **补样最大化**：专题缺额为左部槽位、备选票为右部点的二部图，Kuhn 增广路求最大匹配；
  专题按登记顺序、备选按登记顺序处理，结果确定。补上的票在所有专题的备选条目中同时失效，
  保证每张票只占一个名额。
- **并发与原子性**：每批处理（查重 → 整批退出 → 算缺额 → 匹配 → 写回执）包在一个
  `BEGIN IMMEDIATE` 事务里，SQLite 写锁把并发批串行化，等价于按生效顺序逐批处理；
  事务内任何异常整体回滚。
- **幂等**：批次的票号集合排序去重后作为幂等键；同键重送直接返回库存回执原文，异键 409。
- **登记校验**：专题代码唯一、名单内不重复、样本数不超名额、同一票号不得跨专题任样本、
  不得既为样本又为备选（备选跨专题共享是允许的）。

## 测试与试跑

```bash
.venv/bin/python -m pytest -q          # 31 项：功能 + 回滚 + 重启持久化 + 真并发一致性 + 回执核查
.venv/bin/python scripts/demo.py http://127.0.0.1:8000         # 现场试跑：共享备选/并发冲红/幂等/冲突
.venv/bin/python scripts/audit_demo.py http://127.0.0.1:8000   # 现场试跑：共享备选不足的批次怎么核查
```

`audit_demo.py` 会登记一轮 8 个名额、仅 4 张共享备选的清册，整批冲红后核查：打印缺额组与
核等式，然后提交后续冲红批，再查同一张回执，断言两次结果逐字节一致。

并发一致性测试方法：10 个不同批次经真实 HTTP 服务并发提交后，按跑出的生效顺序 seq
在全新数据库上串行重放，断言两者回执与最终名单完全一致；另有同批号同集合并发重送
（仅一次生效）与同批号异集合并发（仅一次生效、其余 409）的用例。
`tests/test_audit.py` 另含随机属性测试：30 组随机专题/共享备选拓扑、每组 4 批随机冲红，
逐个回执断言核查等式成立。

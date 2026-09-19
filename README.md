# 奶具清洗消毒放行

本项目管理奶瓶、奶嘴、盖件和装载架从回收到清洗、消毒、封存与发放的过程。系统使用 Python（FastAPI + SQLite），关联设备周期曲线、放行决定和失效后的去向核查。

## 领域模型

- **奶具 Item**：`item_code` 唯一身份，类型为 `bottle / nipple / cap / rack`；奶瓶可带 `set_id`，放行时校验同批次内套装（奶瓶+奶嘴+盖件）完整。
- **事件 ItemEvent**：回收 → 拆分检查 → 预洗 → 清洁检查 → 装载 → 消毒 → 干燥 → 封存 → 放行 → 发放，全量留痕；扫码事件按 `idempotency_key` 去重，重传不重复推进状态。
- **装载批次 Batch**：`open → cycle_recorded → released / quarantined → invalidated`。同一奶具不能同时处于两个批次（`current_batch_id` 原子占位）。
- **周期曲线 CycleUpload**：设备温度-时长曲线绑定到批次，每次上传留历史；按「≥90°C 连续保温 ≥600s」评估（线性插值越界点）。
- **放行决定 Decision**：每次判定（放行/隔离/失效）记录原因、审批人、触发源，历史不可改。
- **发放 Distribution**：批次 → 房间 → 奶具，`in_room / used / recalled`。
- **事项 Task**：批次失效后自动生成——仍在房间的生成 `recall`（回收），已使用的生成 `followup_check`（后续核查）。

## 关键规则

| 规则 | 行为 |
|---|---|
| 部件缺失 / 清洁检查未通过 / 周期参数不达标 / 封存破损 | 不得放行；放行尝试被阻断时批次与奶具进入隔离 |
| 未经预洗（或清洁未过）的配件 | 装载即被拒绝（`ITEM_NOT_READY`） |
| 设备数据迟到 | 触发重新判定并留痕：已放行且新评估不达标 → 自动失效并传播；**已隔离的批次隔离不被抹掉** |
| 同一奶具并发装入两个批次 | `BEGIN IMMEDIATE` + 条件 UPDATE 原子占位，只有一个成功 |
| 扫码重传 | 同一幂等键返回原事件；换键重复推进状态返回 409 |
| 放行后发现周期无效 | 沿发放记录传播：在房间 → 回收事项并召回，已使用 → 后续核查事项，未发放 → 隔离 |

## 运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload          # 数据库路径用 MILK_DB 环境变量指定，默认 ./milk.db
```

## 测试

```bash
python -m pytest            # 16 个用例，覆盖并发装载、迟到设备数据、批次失效传播
```

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/items` | 注册奶具（唯一编码、类型、套装） |
| GET | `/items/{code}` | 奶具状态与完整事件流水 |
| POST | `/items/{code}/events` | 扫码事件（`event_type`/`payload`/`idempotency_key`） |
| POST | `/batches` | 创建装载批次（绑定设备） |
| GET | `/batches/{code}` | **主管视图**：装载明细、关键曲线与评估、未通过原因、审批人、受影响去向与事项 |
| POST | `/batches/{code}/load` | 装载奶具（并发安全、同批幂等） |
| POST | `/batches/{code}/cycle` | 上传设备温度曲线（迟到数据自动触发重新判定） |
| POST | `/batches/{code}/release` | 放行（需 `approver`；阻断原因存在时 409 并隔离） |
| POST | `/batches/{code}/invalidate` | 判定周期失效，沿发放记录生成回收/核查事项 |
| POST | `/batches/{code}/distribute` | 发放到房间（幂等） |
| POST | `/distributions/{id}/use` | 标记已使用 |
| GET | `/tasks` · POST | `/tasks/{id}/complete` 事项查询与闭环 |

## 代码结构

```
app/
  db.py        SQLite 连接与建表（WAL + BEGIN IMMEDIATE 写事务）
  service.py   业务逻辑：状态机、曲线评估、放行判定、隔离、失效传播
  main.py      FastAPI 路由与错误处理
tests/
  test_pipeline.py      主流程与四类放行阻断
  test_concurrency.py   并发装载同一奶具/多奶具
  test_late_data.py     迟到设备数据的重新判定与隔离保持
  test_invalidation.py  批次失效传播与事项闭环
```

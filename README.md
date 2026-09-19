# 奶具清洗消毒批次放行服务

为奶瓶（bottle）、奶嘴（nipple）、盖件（cap）、装载架（rack）建立唯一扫码身份，
记录 **回收 → 拆分检查 → 预洗 → 设备装载 → 消毒周期曲线 → 干燥 → 封存 → 放行发放 → 使用**
全过程事件，并把设备保温温度/时长曲线绑定到具体装载批次。部件缺失、清洁检查未通过、
周期参数不达标或封存破损一律不得放行；设备数据迟到会触发重新判定，但不抹掉已采取的
隔离；放行后发现周期无效，系统沿发放记录找出仍在房间与已使用的奶具，分别生成
**回收（recall）** 与 **后续核查（investigation）** 事项。

技术栈：FastAPI + SQLite（标准库 `sqlite3`，无 ORM）。

## 快速开始

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 晨间场景演示（内存库，直接看主管视图）
.venv/bin/python demo.py

# 启动 HTTP 服务（STERILIZER_DB 指定数据库文件，默认 sterilizer.db）
.venv/bin/uvicorn app.main:app --reload
# 打开 http://127.0.0.1:8000/docs 查看交互式接口

# 测试（含并发装载、迟到数据、失效传播）
.venv/bin/python -m pytest -q
```

## 领域规则

| 规则 | 实现方式 |
| --- | --- |
| 唯一身份 | `items.code` 主键，四类器具统一建模 |
| 事件只追加 | `item_events` 追加式事件流，状态由最新事件推导，历史永不覆盖 |
| 扫码重传幂等 | `UNIQUE(item_code, idem_key)`，同一幂等键返回既有事件，`advanced=false` |
| 设备消息重传幂等 | `device_messages.message_id` 去重，重传返回 `duplicate=true` |
| 不能同时在两个批次 | `batch_members` 上的部分唯一索引 `… WHERE active=1`，并发下由数据库仲裁 |
| 部件缺失/清洁不通过 | 拆分检查即追加粘性隔离事件，隔离期间不能装载/预洗 |
| 未经预洗混入 | 允许先上架（还原抽查场景），闸门以 `not_prewashed` 拦截，复核可撤出并隔离 |
| 周期不达标 | 保温 ≥93℃ 且持续 ≥300s、干燥 ≥300s；曲线缺失或参数不足即拒绝放行 |
| 封存破损 | 破损件立即隔离，闸门以 `seal_broken` 拦截 |
| 迟到设备数据 | 设备侧时间早于服务端收到时间 >10 分钟标记 `late`；到后自动重判；**合格迟到曲线不能清除既有隔离**，隔离只能经返工 `dequarantine` 解除 |
| 放行后周期失效 | 批次置 `invalidated`；沿发放事件定位去向：未使用→`recall`+追回事件，已使用→`investigation`；未发放在架（含装载架）→隔离待返工；事项 `(批次,奶具,类型)` 唯一，重复失效幂等 |
| 装载架 | 消毒间器具，可被隔离返工，但不随批发放到病房、不产生去向事项 |

## 主要接口

```
POST   /items                                 登记奶具
GET    /items/{code}                          奶具当前状态/是否隔离
POST   /items/{code}/collect                  回收        {idem_key}
POST   /items/{code}/split-check              拆分检查    {idem_key, passed, parts_complete}
POST   /items/{code}/prewash                  预洗        {idem_key}
POST   /items/{code}/quarantine|dequarantine  隔离/返工解除
POST   /items/{code}/used                     房间内使用登记

POST   /batches                               建批次（绑定设备+装载架）
POST   /batches/{id}/members/{code}           装载奶具（重复装载 409）
POST   /batches/{id}/members/{code}/remove    装载复核撤出（可附隔离原因）
POST   /batches/{id}/complete-loading         完成装载
POST   /batches/{id}/curve                    上传设备保温曲线（支持迟到/重传）
POST   /batches/{id}/dry                      干燥登记
POST   /batches/{id}/seal                     封存（可逐件标注破损）
POST   /batches/{id}/evaluate                 闸门重新判定
POST   /batches/{id}/release                  放行审批 {approver, destination}
POST   /batches/{id}/invalidate               事后判定周期无效并传播
GET    /batches/{id}                          主管视图（见下）
GET    /batches/{id}/events                   批次完整事件流水（审计）
GET    /followups?status=open                 回收/核查事项列表
POST   /followups/{id}/resolve                核销事项
```

`GET /batches/{id}` 主管视图包含：装载明细（含撤出记录、每件状态与去向）、
关键曲线（温度/时长采样、阈值、是否迟到）、最近一次闸门判定的未通过原因、
审批人与审批结论、失效原因，以及受影响的回收/核查事项。

## 典型流程（curl）

```bash
# 前段工序
curl -s -XPOST localhost:8000/items -H 'Content-Type: application/json' \
  -d '{"code":"B-1","item_type":"bottle"}'
curl -s -XPOST localhost:8000/items/B-1/collect -d '{"idem_key":"s1"}'
curl -s -XPOST localhost:8000/items/B-1/split-check \
  -d '{"idem_key":"s2","passed":true,"parts_complete":true}'
curl -s -XPOST localhost:8000/items/B-1/prewash -d '{"idem_key":"s3"}'
# …装载架 R-1 同法准备…

curl -s -XPOST localhost:8000/batches -d '{"batch_id":"LOT-1","device_id":"DEV-1","rack_code":"R-1"}'
curl -s -XPOST localhost:8000/batches/LOT-1/members/B-1
curl -s -XPOST localhost:8000/batches/LOT-1/complete-loading
curl -s -XPOST localhost:8000/batches/LOT-1/curve -H 'Content-Type: application/json' -d '{
  "message_id":"dev-msg-1","device_id":"DEV-1",
  "hold_samples":[[0,95.0],[300,95.2]],"drying_seconds":300}'
curl -s -XPOST localhost:8000/batches/LOT-1/dry
curl -s -XPOST localhost:8000/batches/LOT-1/seal -d '{}'
curl -s -XPOST localhost:8000/batches/LOT-1/release \
  -d '{"approver":"主管甲","destination":"302病房"}'
```

## 测试

- `tests/test_service.py`：闸门各类拦截、迟到曲线不抹隔离、幂等、失效传播、主管视图
- `tests/test_concurrency.py`：多线程并发装载同一奶具（跨批/同批）、并发重传、真实迟到数据
- `tests/test_api.py`：HTTP 端到端与错误码

并发正确性依赖：所有写事务使用 `BEGIN IMMEDIATE`，文件库开启 WAL + `busy_timeout`，
占用冲突最终由部分唯一索引裁决（一个成功、一个 409，绝不出现双重占用）。

# 科研载荷海试验收服务

按试航科目关联装船仪器、校准证书、海上观测时间、可复现实验记录与样本交接，支撑民资科考船首次海试的载荷协同验收。核心约束见 `docs/domain.md`：

- 一个科目未完成，不能冒用其他航次的合格结论（证书/观测/实验均按航次判定）；
- 异常数据保留原始读数，且必须登记复核人后科目才能完成；
- 船东只看交付条件，研究机构在授权范围内追溯样本，验收人员可指出每项仪器在哪段航行中完成验证及复测原因。

本服务仅依赖 Python 标准库，持久化使用 SQLite 本地文件。运行参数 `PORT` 指定监听端口，`DATABASE_PATH` 指定数据文件。

## 本地开发

```bash
make migrate     # 初始化/升级数据库结构
make seed        # 可选：写入演示航次与四个角色令牌（虚构数据）
make test        # 运行自动化检查
make run         # 启动 HTTP 服务（默认 :8080）
```

`docker compose up --build` 可启动隔离容器，`APP_PORT` 可调整宿主机端口。

## 角色与鉴权

所有业务接口要求 `Authorization: Bearer <token>`，令牌及其角色存于 `access_tokens` 表（演示令牌由 `make seed` 写入：`SCI-TOKEN` / `ACC-TOKEN` / `OWNER-TOKEN` / `RI-TOKEN`）。

| 角色 | 权限 |
| --- | --- |
| `scientist` 海洋科研团队 | 全部录入、异常复核、样本授权 |
| `acceptance` 靠港验收人员 | 科目完成/判退、仪器验收结论、完整报告 |
| `owner` 船东 | 仅交付条件视图 |
| `researcher` 研究机构 | 授权范围内样本溯源 |

## 接口一览

录入类（科研团队）：

| 方法与路径 | 说明 |
| --- | --- |
| `POST /voyages` | 建立航次 |
| `POST /voyages/{id}/legs` | 增加航段（seq 可自动生成） |
| `POST /instruments` | 登记装船仪器 |
| `POST /certificates` | 登记本航次校准证书（过期证书拒绝） |
| `POST /subjects` | 建立试航科目并关联仪器 |
| `POST /observations` | 登记海上观测时间窗（航段必须属于科目航次） |
| `POST /experiments` | 登记可复现实验（规程版本 + 参数快照） |
| `POST /experiments/{id}/readings` | 批量追加读数；异常读数须附复核信息 |
| `POST /readings/{id}/review` | 事后复核异常读数（只能登记一次） |
| `POST /samples` | 登记样本来源 |
| `POST /samples/{id}/handovers` | 追加样本交接记录 |
| `POST /grants` | 向研究机构发放单样本/整科目的追溯授权 |

验收类：

| 方法与路径 | 说明 |
| --- | --- |
| `POST /subjects/{id}/complete` | 完成科目；证据不全返回 422 与 blockers |
| `POST /subjects/{id}/fail` | 判退科目 |
| `POST /validations` | 登记仪器逐航次结论（待复测/不通过必须填原因） |
| `GET /voyages/{id}/report` | 完整报告：每台仪器的验证航段、证据核查与复测原因 |
| `GET /voyages/{id}/delivery` | 船东视图：仅"满足交付/暂不满足交付" |
| `GET /samples/trace` | 研究机构视图：授权样本来源与交接链 |

时间字段一律使用带时区偏移的 ISO 8601 字符串。字段与判定规则见 `contracts/entities.json`，交换示例见 `fixtures/example.json`。

## 快速演练

```bash
make seed && make run
# 验收报告（哪段航段验证、为何复测）
curl -s -H "Authorization: Bearer ACC-TOKEN" \
  http://localhost:8080/voyages/VOY-2026-01/report
# 船东只看交付条件
curl -s -H "Authorization: Bearer OWNER-TOKEN" \
  http://localhost:8080/voyages/VOY-2026-01/delivery
# 研究机构追溯被授权样本
curl -s -H "Authorization: Bearer RI-TOKEN" \
  http://localhost:8080/samples/trace
```

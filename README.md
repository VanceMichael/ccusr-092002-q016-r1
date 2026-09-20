# 科研载荷海试验收服务

渔民共同出资的科考船海试时，需在实际海况中协同验证走航采样仪、冷藏样本舱与定位设备。本服务按**航次 → 航段 → 试航科目**组织装船仪器、校准证书、海上观测时间、可复现实验记录、原始读数与样本交接链，并提供分角色验收视图。

- 科目的合格结论只能由**同一航次**的证据支撑，禁止借用其他航次结论；
- 异常数据保留**原始读数**，复核人靠港后补登，未复核不得判合格；
- 船东只看仪器是否满足交付条件；研究机构仅在授权范围内追溯样本来源；验收人员可指出每件仪器在哪段航行完成验证及复测原因。

技术栈：Python 标准库 HTTP 服务 + SQLite，无第三方运行时依赖。运行参数 `PORT` 指定监听端口，`DATABASE_PATH` 指定数据文件。

## 本地开发

```bash
make migrate   # 初始化/升级数据文件
make test      # 运行自动化检查（含端到端 HTTP 流程测试）
make run       # 启动服务
```

`docker compose up --build` 可启动隔离容器，`APP_PORT` 调整宿主机端口。

## 鉴权

所有业务接口需携带请求头：

| 头 | 含义 |
| --- | --- |
| `X-Actor-Role` | `coordinator` / `inspector` / `researcher` / `shipowner` |
| `X-Actor-Ref` | 操作者或研究机构标识；样本追溯按该标识做逐样本授权 |

> 当前实现为演示级角色头鉴权（无签名），生产部署应置于 API 网关/认证代理之后。

## 接口一览

建档（coordinator/inspector）：

- `POST /voyages`、`POST /voyages/{id}/legs`、`POST /voyages/{id}/subjects`
- `POST /instruments`、`POST /instruments/{id}/certificates`
- `POST /subjects/{id}/instruments`（仪器必须属于航次所在船舶）
- `POST /samples/{id}/access-grants`（仅 coordinator，逐样本授权研究机构）

现场记录（coordinator/inspector/researcher）：

- `POST /subjects/{id}/windows`（航段必须属于同航次，否则 409）
- `POST /subjects/{id}/runs`（可复现实验：程序编号 + 参数 + 操作人 + 时间）
- `POST /runs/{id}/readings`（`raw_value` 原始读数；`anomaly=true` 可先无复核人）
- `POST /readings/{id}/review`（异常读数补登复核人与说明，原始值不可改）
- `POST /runs/{id}/samples`、`POST /samples/{id}/handovers`

验收（仅 inspector）：

- `POST /subjects/{id}/decision`
  - `{"status":"PASSED","decided_by":...}`：证据/校准/复核不齐时返回 422 并列出阻断项；
  - `{"status":"RETEST_REQUIRED","decided_by":...,"retest_reason":...}`：复测原因必填。

视图：

- `GET /voyages/{id}/delivery`（shipowner/inspector/coordinator）：逐仪器 `delivery_ready`，不含研究数据；
- `GET /voyages/{id}/acceptance`（inspector/coordinator）：每件仪器在各科目中完成验证的航段与观测时间、结论、复测原因；
- `GET /samples/{ref}/provenance`、`GET /samples/{ref}/readings`（仅 researcher）：授权机构可见来源航次/科目、可复现实验要素、交接链、原始读数与异常复核信息；未授权 403。

其他约定见 `contracts/entities.json`（字段约定）、`docs/domain.md`（领域规则）、`fixtures/example.json`（脱敏交换示例）。

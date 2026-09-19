# 民资科考船试航条件闭环

科考船试航依赖建造检验、航行水域及气象警报等多方材料，海事部门负责许可和在航提醒。

本服务采用 HTTP 接口和 SQLite 本地文件。运行参数 `PORT` 指定监听端口，`DATABASE_PATH` 指定数据文件；`fixtures/example.json` 保存不含真实身份的交换示例，`contracts/entities.json` 记录字段约定，`docs/domain.md` 介绍来源与范围。

## 本地开发

`make migrate` 初始化数据文件，`make test` 运行现有自动化检查，`make run` 启动服务。`docker compose up --build` 可以启动隔离容器，`APP_PORT` 可调整宿主机端口。

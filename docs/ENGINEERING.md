# 工程与代码标准

适用范围：本仓库的新代码及本轮修改部分。维护日期：2026-09-06。
这是结合当前代码与交付方式制定的项目约定；不代表已获得外部标准认证，也不要求一次性重排存量代码。

## 架构边界

| 目录/文件 | 责任 |
|---|---|
| `librenms+grafana/platform_api/` | 认证、读写 API、配置事务、Apply 生命周期、事故与网络工具 |
| `librenms+grafana/bigscreen/` | 原生 JavaScript 页面、控制器、模型和图表；当前无 npm 构建链 |
| `librenms+grafana/feishu_bridge/` 及 bridge 入口 | 监控事件、通知和设备生命周期 |
| `librenms+grafana/*targets*.py`、发现脚本 | 设备发现、采集目标和拓扑生成 |
| `librenms+grafana/docker-compose.yml`、部署脚本 | 服务组装、配置生成、部署与健康验收 |
| `librenms+grafana/tests/` | Python/JavaScript 回归测试与脱敏样例 |
| `.github/workflows/ci.yml` | 已启用的自动验证，具体版本和命令以此为准 |

新增逻辑优先放入对应领域模块，入口只做编排。不要把业务逻辑堆进大屏入口或新增一套重复配置模型。
配置/API/持久化格式修改必须说明旧数据如何读取、何时写回、失败如何保留原数据；必要时提供迁移及恢复步骤。

## 代码约定

- 编码 UTF-8、换行 LF、文件末尾换行，遵循 `.editorconfig` 和 `.gitattributes`。只格式化本次修改范围。
- Python 使用 4 空格、函数/变量 `snake_case`、类 `PascalCase`、常量大写。新增公共边界写清输入输出类型；异常在能处理它的边界转换，保留安全的错误语义。
- JavaScript 使用 2 空格、现有引号/分号和模块导出风格；优先 `const`，必要时 `let`，不引入无关框架或打包工具。DOM 渲染、数据模型、请求编排分离。
- Shell 遵循声明的解释器，不在 `sh` 脚本加入 Bash 专用语法。变量和路径必须正确引用；检查退出码、管道失败、超时与子进程清理；按脚本语义使用严格模式，不能仅靠 `set -e` 推断成功。
- YAML/JSON 遵循现有结构；示例配置与运行配置分离。新增配置说明默认值、单位、范围、兼容 alias 及冲突策略。
- 命名表达业务含义，注释解释原因、边界和不变量，不复述代码。避免大段重复逻辑、无关依赖及未使用的抽象。
- 新增依赖要有必要性、版本兼容与部署影响说明，同步实际安装入口和对应测试；不得擅自全面升级现有依赖或增加格式化门禁。

## 正确性与安全

- 外部输入先校验后使用：文件、JSON、IP/CIDR、HTTP body、查询参数均要考虑规模上限和异常类型。禁止先完整分配再检查上限。
- I/O 和命令调用有超时；循环/轮询有终止条件和总期限，避免各步骤重置预算后无限延长。需要时隔离 stdin。
- 写操作先构造并验证完整候选，再执行原子替换或事务；失败不得部分更新。历史超限数据不自动裁剪、删除或重建。
- 前端异步任务检查生命周期代次及提交顺序，离开页面/退出/新登录/Apply 时废弃旧响应；不得破坏草稿、operationId、恢复轮询或期限语义。
- 授权在实际写入口执行；UNKNOWN/探测失败不视作离线；在线证据优先。公司删除安全要求见 [项目约束](PROJECT_CONTEXT.md)。
- 日志与错误返回说明失败阶段和安全错误类别，不输出 token、密码、完整配置、服务器敏感路径或事故内容；不要通过成功日志掩盖失败。
- 部署成功必须有本轮任务容器和健康检查证据；静态语法通过不等于运行验收。

## 验证标准

新增功能与缺陷修复优先补可复现的行为测试，覆盖成功、失败和关键边界；异步问题使用可控时钟/请求顺序，容量问题验证超限前终止和原数据保留。
禁止测试访问生产设备、发送真实通知或执行真实 DELETE。不要为纯文档、格式变更编写复述实现的测试。

从仓库根目录执行下列已有入口，按变更选择，记录工具版本和结果：

| 变更 | 最低针对性验证 |
|---|---|
| Python 逻辑 | `python -m pytest -q librenms+grafana/tests/test_<相关模块>.py`；修改文件语法检查 |
| 控制台生命周期 | `node librenms+grafana/tests/test_bigscreen_auth_controller.js`、`test_bigscreen_config_editor.js`、`test_bigscreen_apply_recovery.js`（后两项同目录，均使用 node 执行） |
| 前端其他模块 | 对应 `test_bigscreen_*.js`，修改 JS 使用 `node --check <文件>` |
| 部署/Shell | 声明解释器的 `-n`、`shellcheck --severity=error <脚本>`、相关部署 Python 测试 |
| Compose/配置 | 隔离测试目录使用 example 配置执行 `docker compose config --quiet`；禁止覆盖真实 `.env` 或 `event-config.yml` |
| 纯文档/编辑器规范 | diff 检查、相对链接存在性、命令/源码路径核对、规则冲突及敏感内容检查 |

需要全量验证时，CI 当前采用 Ubuntu、Python 3.13、Node 20，覆盖 pytest、Python/JS/Shell 语法、ShellCheck error、Compose、Linux static smoke 和 dashboard JSON。
开发依赖入口为 `librenms+grafana/requirements-dev.txt`；Python 全量为 `python -m pytest -q`。
已有 CI 保留；不得为了“变绿”删除断言或降低门槛。测试契约确已改变时先确认需求和行为，再更新过期断言。

本地缺工具/版本不符：记录“未运行/非目标环境”，继续可做的验证和普通 push，随后核对 CI。
已运行失败：先判定是否本次回归，修复范围内真实阻断；既有或环境故障写明证据、影响和后续动作，不称为通过。
检查通过后，只有新修改、新失败或未解决疑点才扩大/重复测试。

## 完成定义

- 范围与验收条件满足，关键失败路径有证据，最终 diff 完成自查。
- 相关文档、迭代记录和 STATUS 同步；未测项和风险明确。
- 普通 commit、push 和远端核验完成；否则只能报告对应的部分交付状态。
- 需要部署时交付完整操作手册；生产待验收期间保留活跃批次。
- 用户确认服务器与网页验收后，追加记录并提交，才可将该业务批次标记“已收口”。

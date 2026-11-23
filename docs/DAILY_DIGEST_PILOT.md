# AWS Lambda 每日摘要试点

状态为本地验证。负责人大肥鱼主控。新增费用上限 $0，银行卡与 AWS 抵扣金均不扣。继续使用现有设备。QQ、微信、Swift 和悉尼 HY2 保持原样。

## 本地验证

使用 Python 标准库，只读 model-hub 用量账本的指定上海自然日日分桶，默认读取昨天。输出当日输入、输出、总 token 数、请求次数和缺用量次数。账本的提供商和模型名称、请求标识、会话数据及其他字段均不会写入摘要。

在仓库里选择一份本地 schema 2 测试账本和独立输出目录，然后运行：

```text
python -B integrations/pi-console/model-hub-usage/daily_digest.py --ledger ./fixtures/usage-ledger.json --cache-dir ./local-digest --date 2026-11-23
python -B -m unittest discover -s integrations/pi-console/model-hub-usage -p "test_*.py"
```

正常输出 `fresh` 并原子写入对应日期的摘要。读不到账本、格式损坏或当天缺数据时，若有当日已验证缓存则返回 `cached`，否则返回 `unavailable` 且退出码为 2。写缓存失败时仍输出可用的聚合并标明写入失败。命令可以在无网络环境运行。

## 后续清单

- [x] 本地验证日期聚合、结构脱敏、故障回退、原始账本字节保留。
- [ ] 确认实际 AWS 账户和目标区域的 Lambda、日志当月免费额度，以及其他用量对额度的影响。只有能满足长期 $0 和零抵扣条件，才提交正式 AWS 资源创建清单并等待确认。
- [ ] 云端条件通过后，在现有设备上触发一次受控调用。传入预先脱敏的聚合数据。只有云端路径和本地回退都能重复验证时再讨论每日自动运行。

## 云端资源与费用核对草案

| 拟用资源 | 最小设想 | 费用核对 |
| --- | --- | --- |
| AWS Lambda 函数一项 | 128 MiB、单次最多 3 秒、每日一次。 | 按 31 天估算每月 31 次请求和至多 11.625 GB·秒，不计重试。须与账户实际可用免费额度比对。 |
| CloudWatch Logs 日志组一项 | 只写固定结果，短保留。输入、摘要明细及异常原文不写到云端日志。 | 拟按每次最多 4 KiB 估算，每月约 124 KiB 写入，另按账户计入存储。该估算不构成硬性额度。 |
| IAM 最小权限执行角色一项 | 仅允许函数写本试点指定的日志组。 | 核对调用方式与权限边界后再纳入资源清单。 |

AWS 官方公布的 Lambda 月度免费额度为 100 万次请求与 40 万 GB·秒，CloudWatch Logs 列出 5 GB 数据额度。AWS Budgets 有计量与告警延迟，因此告警本身不足以证明试点可以保证 $0。账户级额度与账单尚未核验，试点保持本地。 [AWS Lambda 价格](https://aws.amazon.com/lambda/pricing/) [Amazon CloudWatch 价格](https://aws.amazon.com/cloudwatch/pricing/) [AWS Budgets 延迟](https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-managing-costs.html)

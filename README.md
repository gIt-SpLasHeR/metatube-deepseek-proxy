# metatube-deepseek-proxy

让 MetaTube 用 DeepSeek 翻译时**关闭思考**的反向代理，外加记账和批量补翻工具，用 docker 运行。

## 为什么需要它

- `deepseek-flash`（DeepSeek-V4.1-Flash）默认开启思考。翻一个 40 字左右的标题，思考要花约 1400 个 token（按输出计费），耗时 5–25 秒。
- MetaTube 的设置里只有 api url、key 和模型名三项，没法传 DeepSeek 的 `thinking` 参数。
- 本代理给每个 `/chat/completions` 请求补上 `"thinking": {"type": "disabled"}`，其他内容原样转发给 `https://api.deepseek.com`。如果客户端自己带了 `thinking`，就保留客户端的设置。
- 代理**不保存 key**，直接透传请求里的 `Authorization` 头。
- 每次调用的 token 用量写到 `data/usage.jsonl`，只记数字，不记翻译内容。

2026-09-28 按 MetaTube 的真实请求格式实测（20 个标题 + 9 篇简介）：关掉思考后，输出 token 从 58758 降到 4501，每次耗时 1 秒左右。质量上的代价是：约 5% 的标题会被译成英文（以片假名外来词开头的标题尤其容易），另外人名偶尔会被硬配汉字。

## 部署

```sh
git clone <本仓库地址> metatube-deepseek-proxy
cd metatube-deepseek-proxy
docker-compose up -d --build
curl -s localhost:8765/healthz   # 返回 ok 就是在跑了
```

更新代码：`git pull && docker-compose up -d --build`

私有仓库建议在部署机器上配一把只读的 deploy key 来拉取。

## 接入 MetaTube

在 Jellyfin → 控制台 → 插件 → MetaTube 里设置：

| 项目 | 值 |
|---|---|
| Translation engine | OpenAI |
| OpenAI api url | `http://<代理所在主机的局域网 IP>:8765/v1` |
| OpenAI model | `deepseek-flash` |
| OpenAI api key | DeepSeek 的 key |

翻译请求实际是 MetaTube 服务器发出的，不是 Jellyfin，所以 api url 要写 MetaTube 服务器能访问到的地址，不能写 localhost（除非两者在同一台机器上）。

## 用量与费用

```sh
docker exec deepseek-proxy python report.py /data/usage.jsonl
```

按天汇总请求数、token 数和估算费用。单价写在 `report.py` 里，取自 2026-09-28 的官方价格页（最近一次调价是 9/10）。DeepSeek 调价很频繁，对账前请先重新核对单价。

注意：DeepSeek 的余额扣费会延迟 1–3 分钟，而且分几次入账，刚调用完就看余额会少算。

## 批量补翻已刮削的日文标题（`tools/batch_translate.py`）

用来处理 MetaTube 刮削过、但标题（和简介）还是日文的片子。它只改 Jellyfin 条目的 **Name 和 Overview** 两个字段，类型、演员、图片、合集都不动，所以比逐部强制刷新安全。

- 标题按 MetaTube 的默认模板拼成 `{番号} {译文}`。
- 通过代理翻译，默认不思考，提示词和 MetaTube 发的完全一样。
- 每条译文都会检查，有下面任何一种情况就算不合格：
  - 汉字数不到原文日文字符数的 30%，说明被译成了英文，或者整句被丢掉了。英文品牌词不影响这项判断。
  - 假名占比过高，说明原样返回了日文。
  - 出现繁体字。
  - 出现原文里没有的拒绝话术。
- 不合格的条目会单独开思考、明确要求简体中文重翻一次；还不行就跳过，写进日志。
- 处理范围：
  - 标题仍含日文原标题的片子，包括被早先的翻译弄乱、后面拼着注释之类杂质的标题；
  - 以前跑过、但按现在的规则检查不合格的片子；
  - 在用这个工具之前就翻译过（比如用别的模型或引擎）、但按现在的规则检查不合格的片子，比如繁体、只翻了一半的；
  - 标题已经是中文、但简介还是日文的片子，这种只补简介。
- 每一条处理记录都写进 `data/batch-translate.jsonl`，包括原值和新值，所以可以反复续跑。回滚时恢复的是每部片子**第一次被改之前**的值。
- 只在空闲时段跑：遇到高峰时段（工作日 9–12、14–18 点）会自动暂停。
- 保护措施：前 30 部失败超过 10% 就停；估算费用超过 `BUDGET_CNY`（默认 ¥5）也停。

在 Jellyfin 所在主机的仓库目录里执行（批量工具用主机网络访问 Jellyfin 和代理）：

```sh
# batch.env 已被 .gitignore 排除，里面放 JF_KEY=...（Jellyfin API key）和 DS_KEY=...（DeepSeek key），权限设成 600
tools/run_batch.sh -e DRY_RUN=1          # 只列出要改的内容，不调用翻译、不写入
tools/run_batch.sh -e START_AT=18:00     # 等到北京时间 18:00 再开始
docker logs -f metatube-batch            # 看进度
```

回滚：

```sh
docker run --rm --network host --env-file batch.env -v "$PWD/data:/data" metatube-deepseek-proxy:local \
  python tools/rollback.py --all          # 或者只回滚指定的条目：... rollback.py <itemId> ...
```

## 其他工具

- `tools/quality_check.py`：按 MetaTube 的请求格式把一批标题发给代理，挑出译文不是简体中文的条目。

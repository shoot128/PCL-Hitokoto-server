# 服务端句库来源

`quotes.json` 由一言开源社区官方句库转换而来：

- 上游仓库：<https://github.com/hitokoto-osc/sentences-bundle>
- 上游版本：`1.0.1334`
- 导入时间：`2026-10-24T06:47:14+00:00`
- 当前条数：`10526`
- 去重移除：`660`
- 许可：`AGPL-3.0`

## 字段转换

| 上游字段 | 本服务字段 |
| --- | --- |
| `hitokoto` | `text` |
| `from` | `source` |
| `from_who` | `author` |
| `created_at` | `createdAt`（Unix 秒转 ISO 8601） |
| `type`（单字母分类键） | `type`（中文分类名），`typeCode` 保留原键 |

分类为：动画、漫画、游戏、文学、原创、网络、其他、影视、诗词、网易云、哲学、抖机灵。

## 许可与归属

上游句库采用 **AGPL-3.0**。本目录中的 `quotes.json` 是经过字段改名、分类名转换和去重的衍生数据，继续按 AGPL-3.0 提供。

- 上游 LICENSE 原文：`LICENSE-AGPL-3.0.txt`
- 机器可读来源信息：`SOURCE.json`
- 如果只是通过官方提供的超链接/CDN 调用，请按上游说明处理 AGPL 豁免；自建分发时请遵守 AGPL。

## 重新导入

在服务端项目根目录执行：

```bash
python3 tools/import_sentences_bundle.py
```

脚本会重新下载官方分类，生成 `data/quotes.json`，并更新 `data/SOURCE.json` 和 `data/LICENSE-AGPL-3.0.txt`。旧句库会备份为 `data/quotes.json.bak`。

# ADR 0002：不可变 source_version + 字符区间作为证据合同

- 状态：已采纳
- 日期：2026-10-25

## 背景

"AI 讲了原文没说的话"通常不是模型能力问题，而是数据契约太松：

- 只保存 URL 或摘要，原文更新后无法回查；
- 引用只存 chunk id，无法证明引用的是哪一句；
- 检索把整段塞给模型，模型复述时扩大了条件或删掉了限定语。

## 决策

- 每次成功提取写入一个新的 `source_version`，唯一键是
  `(source_id, content_hash, extractor_version)`；正文与原始快照都保存，不覆盖旧版本。
- 引用一律是四元组：`source_version_id + chunk_id + span_start + span_end`，并附 `quote`。
- 校验是确定性的：`body_text[span_start:span_end]` 必须与 `quote` 逐字相等，且 span 必须落在
  chunk 内、chunk 必须属于该版本、版本必须属于该 owner。
- `chunks.text` 永远是 `body_text[start_char:end_char]` 的切片，切块器被测试强制保证这一点。
- 字符偏移以标准化文本的 Unicode code point 计数，前端高亮使用同一单位。

## 后果

- 好处：引用可以被机器验证，历史节目在原网页改版后仍能回查依据；不需要"信任模型"。
- 代价：存储放大（快照 + 正文 + 切块），每篇文章重新抓取会产生新版本；
  `quote` 与 span 的双重校验让"模型改写后再引用"必须显式携带原文片段。
- 不接受的做法：用检索摘要当证据、用 URL 当版本、用段落序号当引用。

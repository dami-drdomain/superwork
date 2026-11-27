# About SuperWork

> English version of the in-app **About** page (超脑工作搭子 · 关于).

## Application Information

| Item | Value |
|------|-------|
| Application Name | 超脑工作搭子 (SuperWork) |
| Current Version | V1.8.5 |
| Author | Dami (大米) |
| Runtime | macOS · Fully offline local inference (documents never leave this machine) |

## Module Overview

### 📚 Knowledge Base (超脑知识库)
Local, offline hybrid retrieval (vector + BM25 + reranking). Supports incremental indexing, evaluation & verification, backup & restore, full-text search, and automatic document collection.

### ✍️ SuperWork Writer (超脑写手)
Intelligent work-document writing grounded in knowledge-base materials. The output format is determined by the selected skill — Markdown-based skills can be exported to Word; HTML / PPT / Excel skills generate standalone, fully editable files.

### ⚙️ Common Configuration (公共配置)
Retrieval models (loaded locally, offline) and the LLM API endpoint for writing (any OpenAI-compatible service, configured by the user and stored on this machine only).

## Key Privacy Principles

- **Fully on-device**: all document parsing, indexing, embedding, and retrieval run locally; no document content is ever uploaded.
- **Loopback-only service**: the local API binds to `127.0.0.1:18800` and is not reachable from the network.
- **User-owned LLM keys**: the writing LLM endpoint and API key are entered by the user, saved only in the local `llm_config.json`, and never transmitted anywhere except directly to the LLM service you configure.
- **Retrieval audit & kill switch**: all retrieval channels can be audited and disabled at any time.

## Source Code

Open-source repository: https://github.com/dami-drdomain/superwork

See [USER_GUIDE_EN.md](USER_GUIDE_EN.md) for the full English user guide, or [使用说明.md](使用说明.md) for the original Chinese version.

> AI生成

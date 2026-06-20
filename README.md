# CorpusPipeline MetaCheck PUBLIC

<!-- Public-release provenance marker: Scott Leimroth, Copyright 2026. -->

Portable empty starter corpus for ingesting and searching research PDFs.

This public copy contains no private paper database, no source PDFs, no vector DB, and no API keys. A new user creates their own database from scratch.

## Quick Start

1. Double-click `SETUP.bat`.
2. Choose an AI provider when asked.
3. Put PDFs into `papers-staging/`.
4. Double-click `RUN.bat`.
5. After papers are added, run `BUILD_VECTOR.bat` or `UPDATE_VECTORDB.bat` for semantic search.

## AI Provider Choice

The setup asks which AI provider to use.

Recommended for most people:

`OpenRouter`

OpenRouter is a single top-up account that can use cheaper AI models. It supports both normal paper text evaluation and rare scanned/image PDF checks through one key.

Setup explains:

1. Go to `https://openrouter.ai`
2. Create an account
3. Add a small credit amount, such as 5-10 USD
4. Create an API key
5. Paste it into setup

Other choices:

- `DeepSeek`: cheap and good for normal text PDFs, but limited vision/scanned-PDF support.
- `OpenAI`: good text and vision if the user already has an OpenAI key, but usually more expensive.
- `Anthropic`: strong quality if the user already has a Claude/Anthropic key, but usually more expensive.
- `Local/free mode`: no API bill, but requires local AI software and is harder to set up.

If unsure, choose `I'm not sure` in setup and it will explain the choices.

## MetaCheck

This copy includes the Real MetaCheck/GROBID integration.

MetaCheck is a ScienceVerse project for checking research outputs for best-practice signals. ScienceVerse is led by Lisa DeBruine and Daniel Lakens, with broader contributor support. GROBID is used to convert PDFs into structured TEI XML before MetaCheck-style checks run.

For ratable research-paper PDFs:

- GROBID converts the PDF to TEI XML.
- Real MetaCheck runs on the GROBID output.
- The SQLite DB records MetaCheck status and evidence.

For books, translations, supplements, conference items, errata, and other non-ratable items:

- They can still be kept in the corpus where useful.
- They are not rated as research papers.
- MetaCheck is marked `not_applicable`.

For scanned/image/malformed PDFs:

- The corpus pipeline still tries OCR and normal processing.
- If GROBID cannot process the PDF, the DB records `technical_unavailable`.
- This is not a MetaCheck pass or fail; it is a transparent missing-evidence note.

## Main Files

| File/folder | Purpose |
|---|---|
| `SETUP.bat` | Run once on a machine. Installs tools and guides AI provider setup. |
| `RUN.bat` | GUI ingest runner. |
| `RUN_NoGUI.bat` | Console ingest runner. |
| `papers-staging/` | Put new PDFs here. |
| `papers-rejected/` | Files not added to the corpus. |
| `_system/CorpusStore/papers.db` | The user's SQLite database. Starts empty. |
| `_system/CorpusStore/source-pdfs/` | Managed copies of accepted PDFs. Starts empty. |
| `BUILD_VECTOR.bat` | Full vector DB build. |
| `UPDATE_VECTORDB.bat` | Incremental vector DB update. |
| `SEARCH.bat` | Search UI. |

## Privacy And Keys

API keys are stored encrypted in `_system/secrets/anthropic.env.enc`.

The filename is historical; it may contain OpenRouter, DeepSeek, OpenAI, or Anthropic keys. The setup asks the user for a passphrase. The passphrase is not stored.

This public folder should be shared only after confirming:

- no files in `_system/CorpusStore/source-pdfs/`
- empty `papers.db`
- no vector DB
- no logs/cache/backups
- no encrypted or plaintext personal key files

## Attribution And Copyright

This public starter template was prepared by Scott Leimroth.

Copyright 2026 Scott Leimroth. All rights reserved unless a later repository license states otherwise.

This project integrates with, but does not own, MetaCheck, GROBID, Python packages, Docker images, or other third-party tools. Those projects keep their own names, authorship, and licences.

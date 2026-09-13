
# YoMi · A Personal AI Work Assistant inside Feishu

## 1. Overview

<!-- Video: drag an mp4 into this README on GitHub, then replace the src below -->
<video src="https://github.com/user-attachments/assets/6df27ca8-35d5-4c75-9fba-ef54c71b1def" controls width="640" height="480" align="left">
</video>

### Stop writing reports. Start seeing progress.

YoMi turns **"I'll log it later"** into **"already logged."**

A personal work assistant built for Feishu: a **lightweight daily card** arrives at 22:00, a few bullets become
**coherent sentences**, one focused follow-up fills the gaps, and the loop closes with **weekly reports, user
feedback and a monthly capability portrait**.

> **MVP scope:** single user, everything inside Feishu. The LLM layer keeps an **OpenAI-compatible interface**
> and falls back to your raw wording automatically when the model is unavailable.

**Highlights**

- 🪶 **Minimal input**: the card asks only for bullet-style "tasks + outcomes"; optional fields stay collapsed
- ✨ **AI phrasing**: turns scattered points into coherent sentences (≤35 chars), never fabricating
- 🔍 **Gap follow-up**: asks one focused question when outcomes are vague, then re-refines automatically
- ✅ **Confirm / regenerate**: regenerate with feedback, or keep the default saved version
- 📊 **Weekly → feedback → portrait**: fixed-structure weekly report, feedback write-back, monthly summary & capability portrait
- 🔌 **Official SDK over WebSocket**: built on `lark-oapi` long connection — **no public domain or tunneling required**
- 🗣 **Intent routing**: just talk naturally; out-of-scope requests are politely declined

<br clear="all">

[中文文档（Chinese README）](./README.zh-CN.md)

---

## 2. Features

| Capability | Description |
|---|---|
| Daily report | Card pushed at 22:00 daily: bullet inputs, current week goal, collapsible yesterday example |
| AI refinement | Coherent sentences ≤35 chars; falls back to the raw text when AI is unavailable |
| Gap follow-up | One targeted question when outcomes are too vague, then auto re-refine |
| Confirmation loop | Confirm / regenerate with feedback / default save (marked auto-saved at 23:59) |
| Weekly report | Every Saturday 22:00: aggregate the week's dailies + weekly goal → fixed-structure report + scores |
| User feedback | Feedback card (accuracy / usefulness / comments) → **writes only feedback fields, never historical facts** |
| Monthly portrait | Aggregate the month's weeklies → **monthly summary + capability analysis** + 5-dimension scores |
| Goals & standards | AI-suggested weekly goals confirmed by the user; work standards injected into weekly prompts |
| Intent routing | "write daily", "show yesterday's daily", "generate weekly", "show portrait", "suggest next week goals" |
| Data & backup | 5 Bitable tables as the primary store; local JSONL record backups + full snapshots |

### Design principles

- **Clear split between code and AI**: scheduling, validation, aggregation, dedup and period math are deterministic; LLM handles understanding, phrasing and analysis
- **Raw input vs. AI output are stored separately** and fully traceable
- **User feedback wins** over AI self-judgement and never overwrites facts
- **No fabrication**: prompts and parsers constrain the model; missing info stays empty
- **MVP first**: single user, no frontend, no Multi-Agent / RAG / complex memory

### Talk to it in Feishu

```
Hi                        → short self-introduction + capability list
Write daily               → push today's daily report card
Show yesterday's daily    → daily card + Bitable record link
Generate weekly           → weekly report with a feedback form
Show this week's weekly   → weekly card + link
Show my portrait          → latest capability portrait + link
Generate monthly portrait → monthly summary and portrait
Suggest next week goals   → AI candidates, click to save
What are the work standards → team reporting format and requirements
Book me a flight          → politely declined, with the product's scope
```

## 3. Quick Start

### Environment

```bash
conda create -n yomi python=3.12 -y
conda activate yomi
pip install -r requirements.txt
```

### Configuration

```bash
cp .env.example .env      # Windows: copy .env.example .env
```

Fill in: Feishu app credentials, Bitable base & 5 table IDs, recipient `open_id`, and your LLM key.

> Feishu-side setup (custom app, permissions, **long-connection** events & card callbacks, 5 tables) is documented in
> [docs/飞书端配置步骤.md](docs/飞书端配置步骤.md) (Chinese).
> Long connection needs **no public domain or tunneling** — outbound internet access is enough.

### Run

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

The service establishes the Feishu long connection and registers 5 scheduled jobs. Health check: `GET /healthz`

### Scheduled jobs

| Job | Schedule | Notes |
|---|---|---|
| Daily report push | 22:00 daily | Skipped if today's report is already submitted |
| Daily confirm timeout | 23:59 daily | Pending → auto-saved |
| Weekly generation & push | 22:00 Saturday | Aggregates the week, pushes report + feedback card |
| Weekly feedback timeout | 23:59 daily | Out of window → no feedback (timeout) |
| Monthly portrait | 22:00 on day 1 | Generates last month's summary and portrait |

### Verification & ops scripts

```bash
pytest tests -q                                  # unit tests (47)
python -m scripts.e2e_check                      # end-to-end MVP self-check (6 stages)
python -m scripts.manual_trigger daily --dry-run # manual trigger (daily/weekly/portrait)
python -m scripts.seed_demo --dry-run            # demo data (--reset to rebuild)
python -m scripts.backup                         # full backup into data/backups/
```

## 4. Project Layout

```
YoMi/
├─ app/
│  ├─ main.py              # FastAPI entry, lifespan, wiring, callback dispatch
│  ├─ scheduler.py         # Scheduled jobs: daily / weekly / portrait / timeouts
│  ├─ ai/                  # LLM client, prompts, structured-output parsing
│  ├─ core/                # Config, period math, validation, logging, exceptions
│  ├─ data/                # Table schema, Bitable access, local backup
│  ├─ feishu/              # Official SDK wrapper, card builders, long connection
│  ├─ routes/              # Optional HTTP webhook (not needed with long connection)
│  └─ services/            # Orchestration: daily / weekly / portrait / goals / router / queries
├─ scripts/                # Manual triggers, demo data, backup, e2e self-check
├─ tests/                  # Unit tests
├─ docs/                   # Feishu setup guide, etc.
├─ data/                   # Runtime dir: logs / backups (not committed)
├─ .env.example
├─ requirements.txt
└─ README.md (English, default) / README.zh-CN.md (Chinese)
```

## 5. License & Commercial Rights

This project uses a **source-available license with commercial rights reserved**:

- ✅ Permitted: personal learning, research, self-hosting, and non-commercial modification/redistribution with attribution
- ❌ Not permitted without written permission: **any commercial use**, including commercial products or services, paid or free hosted services, internal commercial operations, resale, or bundling into commercial offerings
- 📩 For commercial licensing, customization or partnership, please contact the author

> This is a custom notice, not an OSI-approved open-source license. Before a public release, consider adding a standalone `LICENSE` file and obtaining legal review.

Copyright © 2026 YoMi Project. All rights reserved.

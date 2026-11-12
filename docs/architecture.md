# Architecture & Folder Structure

**依據：** [spec.md](./spec.md)
**原則：** 後端 Clean Architecture（依賴只往內指），前端 Feature-Sliced，前後端以版本化 JSON Schema 為唯一契約。

## 1. 依賴規則

```
interface/http ─┐
                ├──▶ application ──▶ domain
infrastructure ─┘
```

- `domain/` 不 import 任何框架、I/O、subprocess。Impact Propagation、Scenario Discovery、Timeline Builder、Complexity Score 等確定性邏輯都在這裡，可直接 unit test。
- `application/ports/` 定義介面；`infrastructure/` 實作介面；`bootstrap.py` 是唯一組裝依賴的地方。
- `interface/http/` 只做 request/response 轉換，不含業務邏輯。

## 2. Folder Structure

```
pr_review_visualization/
├── schemas/artifact/v1/            # 版本化 JSON Schema（spec §6），前後端唯一契約
│   └── artifact.schema.json        # 單一檔，以 $defs 分區（Node/Edge/Scenario/Timeline/Finding/…）
│
├── backend/
│   ├── src/pr_explorer/
│   │   ├── domain/
│   │   │   ├── model/              # graph, scenario, timeline, evidence, finding, convention, ids
│   │   │   └── services/           # impact_propagation, scenario_discovery, timeline_builder,
│   │   │                           # graph_diff, complexity_score,
│   │   │                           # static_findings（spec §8 可驗證技術 Findings）,
│   │   │                           # convention_inference（Inferred Pattern）,
│   │   │                           # hierarchy, semantic_gate, walkthrough_builder,
│   │   │                           # ask（追問的 digest 與引用檢查：只留這份分析裡存在的 ID）
│   │   ├── application/
│   │   │   ├── ports/              # SourceProvider, LanguageAnalyzer, SemanticAnalyzer,
│   │   │   │                       # KnowledgeSource, ArtifactStore, AnalysisRepository
│   │   │   ├── pipeline/           # spec §5 六層，一層一個 stage
│   │   │   └── use_cases/          # analyze_pr, get_artifact, check_pr_freshness,
│   │   │                           # ask_question（追問：暫時重建 worktree、一次 Claude 呼叫、用完釋放；閒置 worktree 最多保留 10 分鐘供下次重用）
│   │   ├── infrastructure/
│   │   │   ├── source/             # git_worktree, gh_cli
│   │   │   ├── analyzers/
│   │   │   │   ├── generic/        # diff、imports — 所有語言的保底分析
│   │   │   │   ├── python/         # ast_analyzer + django_rules/（urls, views, orm）
│   │   │   │   └── typescript/     # ts_bridge → 呼叫 tools/ts-analyzer（M3）
│   │   │   ├── semantic/           # claude_cli, prompts/, tool_policy（唯讀權限）,
│   │   │   │                       # prompt_builder（repo 內容一律包成 untrusted data，
│   │   │   │                       # 不可成為系統指令，spec §11）
│   │   │   ├── knowledge/          # MCP：Jira, Slack；local_docs（repo 內文件 → Documented
│   │   │   │                       # Convention；使用者補充的文件，spec §3）
│   │   │   ├── persistence/        # sqlite/, artifact_fs, artifact_serializer（domain → schema JSON）,
│   │   │   │                       # schema_validator
│   │   │   ├── security/           # secret_scrubber（寫入 Artifact 前遮蔽 secrets）
│   │   │   └── process/            # 可注入的 subprocess runner（gh / claude / node 測試時換成 fake）
│   │   ├── interface/http/         # FastAPI（docs/api.md），預設 0.0.0.0:8766（`--host` 可改）；有 frontend/dist 時一併 serve
│   │   ├── interface/cli.py        # pr-explorer serve | analyze
│   │   └── bootstrap.py            # Composition root
│   └── tests/
│       ├── unit/                   # 對應 domain/ 與 application/
│       ├── contract/               # Artifact 輸出符合 schemas/ 的驗證
│       ├── integration/            # 真實 git / claude adapter
│       └── fixtures/prs/           # 真實 PR 快照
│
├── tools/ts-analyzer/              # Node CLI + TypeScript Compiler API，stdin/stdout JSON（M3）
│
├── frontend/src/
│   ├── app/                        # App shell（載入、PR 已更新、回填輪詢）、store（主導覽、瀏覽器歷程還原）
│   ├── entities/artifact/          # 由 schemas/ 產生的型別 + selectors
│   ├── features/
│   │   ├── pr-input/               # 輸入、分析進度、完全失敗（原因、重試、保留輸入）
│   │   ├── summary/                # PR 摘要首屏（預設入口）
│   │   ├── changes/                # 變更與影響：具體變更卡（cards.ts 純函式），流程與相關功能在卡片下原地展開
│   │   ├── relations/              # 局部依賴關係圖（local.ts 純函式：≤ 7 節點、分組、關係句）與等價文字清單
│   │   ├── review/                 # 審查重點：三類分開計數、審查卡、明確關聯（related.ts 純函式）
│   │   ├── detail/                 # 按需說明面板（一層、焦點與閱讀位置還原）
│   │   ├── analysis-state/         # 分析狀態矩陣（state.ts 純函式）與狀態提示
│   │   ├── guide/                  # 建議閱讀路徑（預先產生的導覽，原地展開）
│   │   ├── system-map/             # 專案背景：React Flow + ELK.js、領域分層、節點上限、文字清單
│   │   ├── flows/                  # 流程主軸（3–7 段、每段 ≤ 7 步）、選用的動畫與流程地圖
│   │   ├── architecture/           # 介面圈別與依賴規則檢查
│   │   ├── focus/                  # 每頁一個焦點問題
│   │   └── ask/                    # 問 AI 面板（與按需說明面板同一層、同位置；歷史、等待、引用來源、權限）
│   ├── widgets/workspace/          # 主導覽 + 內容區 + 按需說明面板
│   └── shared/                     # ui, api client, lib
│
└── docs/
```

**Runtime 資料**（repo cache、worktree、artifact JSON、SQLite）放在 `~/.pr-explorer/`，不放在專案目錄內，避免 repo 內套 repo 或誤 commit。

## 3. 關鍵設計決定

- **`SemanticAnalyzer` port 只回傳文字欄位**（purpose、rationale、behavior change、convention 說明、semantic findings），不含 nodes / edges / timeline。
  用型別強制 spec §5 Layer 6「Claude 不產生 Graph、Timeline、Code Mapping」。
- **Finding 帶 `source: static | semantic`；Convention 帶 `origin: documented | inferred | hypothesis`。**
  spec §8 的分類由資料模型保證。
- **Graph 元素的狀態拆成三個獨立欄位**（spec §6 分開列出 Change / Impact Status 與 Evidence References）：
  - `change_status`：added / modified / removed / unchanged（spec §4）
  - `impact_status`：confirmed_dependency / potential / evidence_supported / unknown（spec §5 Layer 5）
  - `evidence_refs`：指向 Evidence 的穩定 ID
  Timeline 事件另帶 `evidence_status`（spec §6），未確認的分支必須標示出來（spec §7）。
- **TS 分析器為獨立 Node 程序。**
  TS Compiler API 只能在 JS runtime 執行；對後端仍實作同一個 `LanguageAnalyzer` port。
- **JSON Schema 為唯一真相來源。** 前端型別自動產生；後端 domain 用純 dataclass，由 contract test 驗證輸出符合 schema。
  domain 因此不綁 Pydantic。替代方案是後端也從 schema 生成 Pydantic：少一層 mapping，但 domain 耦合框架。
- **前端不套完整 Clean Architecture。**
  前端只讀取預先生成的 Artifact，沒有業務寫入，分層只會產生空殼。
- **套件管理：** `uv`（Python）、`pnpm`（frontend、ts-analyzer）。

## 4. Milestone 對應

- **M1：** `schemas/`、`domain/`、`application/`、`infrastructure/{source, analyzers/generic, analyzers/python, semantic, persistence, security}`、`interface/http/`、`tests/contract`、前端 `app`、`shared`、`entities/artifact`、`widgets/workspace`、`pr-input`、`overview`、`architecture-map`、`animation-player`、`code-panel`。
  - 與 spec 的差異：spec §12 把「其他語言的通用分析」排在 M3，但 M1 的 Python 分析本身就需要 diff / imports，所以 `analyzers/generic` 提前到 M1。M3 才補齊其他語言的支援。
- **M2：** `scenario-explorer`、`before-after`、`evidence`、`conventions`、`findings`、`walkthrough`、`knowledge/`。
- **M3：** `analyzers/typescript/`、`tools/ts-analyzer/`。

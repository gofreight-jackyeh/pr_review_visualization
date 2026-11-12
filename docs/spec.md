# PR Review Explorer

**Status:** MVP Specification v2（2026-11-11 修訂）
**Deployment:** Local-first
**Primary User:** 不熟悉這個 Repository、業務邏輯與系統架構的工程師
**Core Principle:** 結構來自靜態分析，意義由 Claude 說明，每一句說明都必須有證據

## v2 變更摘要

v1 假設使用者看得懂程式碼、只是不熟這次修改，因此以「追程式碼」為核心（函式層級地圖、逐行動畫、內建程式碼檢視器）。實際使用者連系統做什麼、業務怎麼運作、系統由哪些部分組成都不熟悉，追程式碼反而不是需要的事。v2 的調整：

- 新增 **System Map**：與 PR 無關，先讓人懂整個系統。每個 Repository 建立一次，依 commit 快取。
- PR 視圖改為 **PR Overlay**：在 System Map 上標出這個 PR 動到哪個業務能力與流程。
- 地圖預設停在業務層級（業務能力、業務流程、核心資料），不到函式層級。
- 動畫改為 **業務步驟** 粒度，不再逐行對應程式碼。
- **移除內建 Code / Diff Viewer**。程式碼只作為證據來源，以「檔案:行號」呈現。
- Claude 的業務說明從選配變成核心內容；沒有 Claude 時只呈現系統結構。
- ~~Conventions 降為右側的次要分頁。~~（已撤回，見下方「架構一致性與流程簡化」）
- 名稱與編號：Primary User 從「Junior Engineer」改為「不熟悉 Repository、業務與架構的工程師」；Scenario Explorer 改名為 Business Flow Explorer；新增 Layer 4（System Map Construction），原 Layer 4–6 順延為 5–7。
- 理解測試指標：改以系統用途、業務流程、PR 業務影響為主；v1 的「Architecture / Dependency 理解正確率」併入「主要業務流程」與「Impact」兩項，「Convention 理解能力」移除。
- **架構一致性與流程簡化（2026-11-11 補充）**：Conventions 的原意是「軟體架構上的一致性」，不是程式碼風格，因此改名為「架構一致性」並放回 PR 視角的主要區塊（取代原本的次要分頁）。大型專案的流程改為「主軸」呈現：預設只看業務階段與能力分段，聚焦 PR 改到的段落，每頁只回答一個焦點問題。版面採 concept-map 形式（焦點問題在上、主軸左到右、每格標示可信度）。
- **閱讀順序（UI/UX 優化 P0）**：使用者回報「分析完成後不知道先看哪裡」。入口改為「PR 摘要」，左右兩組獨立分頁合併為一組主導覽（PR 摘要／變更與影響／審查重點／專案背景）；系統概覽改由「專案背景」按需查看。新增分析狀態矩陣、瀏覽器返回與鍵盤操作。
- **理解變更與關係（UI/UX 優化 P1）**：使用者回報「地圖與流程資訊太多，看不懂關係」。具體變更卡成為流程入口，關係圖按需開啟且聚焦目前變更（約 5–7 個節點、附關係句與等價文字清單），固定約 40% 的右側說明欄改為按需面板；審查重點每張卡先寫需確認什麼；「來源」與「確認程度」分開標示。
- **大型專案分層（2026-11-11 補充）**：用一個大型 Django monorepo 實測後發現，一次畫出全部能力完全無法閱讀。新增「業務領域」層（由 Claude 將既有能力歸類，畫面標示來源），過大的能力依子目錄拆開，工程與工具目錄預設隱藏，連往共用基礎的線預設不畫，每個畫面設節點上限，PR 視角只顯示受影響的部分與直接鄰居。

## 1. Project Goal

建立一套互動式 PR 視覺化工具，幫助不熟悉專案的工程師，在收到 Pull Request Review Request 時，**不需要讀程式碼**也能理解：

1. **System** — 這個系統是做什麼的？由哪些部分組成？
2. **Business Flows** — 系統有哪些主要業務流程？
3. **Purpose** — 為什麼需要這次修改？
4. **Behavior** — 這次修改讓哪個業務流程的行為有什麼不同？
5. **Impact** — 哪些其他業務功能可能受到影響？
6. **Review Findings** — 有哪些值得進一步確認的問題？

產品重點是幫助工程師**理解系統與業務**，不是取代 GitHub Code Review，也不是程式碼閱讀工具。

## 2. MVP Scope

### Included

- Localhost Standalone Web Application
- GitHub PR URL Input
- Automatic Repository Clone / Worktree
- **System Map**（Repository 層級，依 commit 快取）
- **Business Flow Explorer**
- **PR Overlay**（在 System Map 上標出這次修改）
- Before / After 業務行為比較
- 業務步驟層級的 Evidence-Gated Animation
- Evidence-Gated Impact Analysis（業務能力與流程層級）
- Review Findings（以檔案:行號指向 PR 改動位置）
- Pre-generated Guided Walkthrough
- Python / Django 深度分析
- TypeScript / JavaScript 深度分析
- 其他 PR 類型的通用分析
- Architecture Consistency（架構一致性檢查）
- SQLite + Local Artifact Storage

### Excluded

- 內建 Code Viewer / Diff Viewer
- 逐行（Statement-Level）程式碼追蹤與同步 Highlight
- Google SSO / RBAC
- Kubernetes 或 Cloud Deployment
- GitHub Review Comment Submission
- PR Approve / Request Changes
- 自動執行 PR 程式碼或測試
- Runtime Debugger
- 動態圖表生成（「問 AI」追問已於 2026-11-12 納入範圍：針對單一 PR 分析結果、唯讀、回答須引用證據並標示為 AI 解讀，預設只限本機使用）
- 自由編輯 Architecture Graph
- Claude 生成可執行的 UI / Animation Code

MVP 必須支援所有 PR 類型進入 Explorer，但不保證所有語言都具備相同的分析深度。

## 3. User Workflow

1. 使用者輸入 GitHub PR URL，可以選擇補充 Jira、Slack 或相關文件。
2. 系統透過 Git / gh CLI 取得 PR Metadata、Diff、Base 與 Head Source Code。
3. 若這個 Repository 在 Base commit 尚未建立 System Map，先建立 System Map；已存在則直接重用。
4. 分析 PR，並把結果疊加到 System Map 上，形成 PR Overlay。
5. Rules + Complexity Score 決定 Claude 說明的深度；System Map 的業務說明與 PR 行為說明都由 Claude 撰寫，並經過證據檢查。
6. 使用者進入 Explorer，預設看到「PR 摘要」：這次為什麼改、主要的修改前後差異、介面變動與待確認事項的計數；需要時再看「變更與影響」「審查重點」或「專案背景」（系統是什麼）。
7. 使用者探索業務流程、播放修改前後的業務步驟、查看影響範圍與待確認事項。
8. 使用者回到 GitHub 完成正式 Code Review。

所有必要的分析與說明在進入 Explorer 前完成生成。

## 4. User Interface

### Visual Design

整體風格：

- 淺色背景
- Isometric Nodes
- Grouped Architecture Map
- 清楚的 Dependency Connections
- 動態路徑 Highlight
- 按需說明面板（寬螢幕並排、較窄筆電為可關閉的覆蓋面板）
- Breadcrumb Navigation
- Zoom、Pan、Search、Layer Filters

所有節點與文字以業務語言為主（例如「訂單」「折扣」「通知」），技術名稱（例如 `orders.services`）作為次要標示。

### Workspace（主導覽）

一組主導覽，每個入口回答一個問題：

- **PR 摘要**（預設入口）：這次為什麼改，主要改變是什麼？
- **變更與影響**：哪些行為改變，與哪些功能有關？（具體變更卡、業務流程主軸）
- **審查重點**：我需要進一步確認哪些問題？（對外介面變動、架構一致性、待確認事項，各自計數）
- **專案背景**：這個系統與各項功能如何運作？（System Map、領域與能力）

**按需說明面板：** 不再固定占用畫面。明確點選功能、步驟或審查項目時才開啟；寬度 ≥ 1440px 時與內容並排，較窄時為可關閉的覆蓋面板。面板標題是目前選取的項目，來源卡片同步標示「目前選取」。一次只有一層：面板內的連結只替換面板內容，不會再開另一個面板。按「關閉」或 Esc 後，鍵盤焦點回到開啟它的按鈕，閱讀位置不變。

**返回與鍵盤：** 主導覽切換與「查看這次修改」「查看〈流程〉」等明確點選才會導覽，並各自成為一筆瀏覽器歷程；返回／前進會還原入口、來源變更卡、展開的區塊與捲動位置（網址只帶 `#入口名稱`）。展開文字不會跳頁。所有操作都可用鍵盤完成，焦點有明顯外框；內文 16px、次要說明 14px，狀態同時以文字或形狀表示，不只靠顏色；以 1366 × 768 筆電視窗確認主要內容可讀可達。

### System Hierarchy

System → Business Domain（業務領域）→ Business Capability（業務能力）→ Business Flow / Core Entity / Entry Point（業務流程 / 核心資料 / 入口）→ Technical Component（技術元件）

- **一次只看一層。** System Overview 預設只顯示業務領域；點進一個領域才展開它的業務能力；點進一個能力，它的流程、核心資料與入口在說明面板中以清單呈現（依種類分組），只有與 PR 相關的才畫成節點。
- **節點上限。** 單一畫面最多約 25 個節點；超過時把其餘節點收合成摘要節點（例如「會計財務：12 個能力」），點擊才展開。
- **連線精簡。** 只畫目前可見節點之間的彙總連線，每個節點只保留權重最高的幾條；其餘連線列在地圖的文字清單中。
- **共用基礎。** 被大多數能力依賴的共用基礎（例如共用函式庫、設定）自成一區；連往它們的線預設不畫，可手動開啟。
- **工程與工具。** 測試、部署、建置設定、文件、腳本等目錄歸為「工程與工具」，預設隱藏，可手動開啟。
- Technical Component（模組、類別、函式）只在使用者主動展開時顯示；展開後也只顯示名稱、一句說明與「檔案:行號」，不顯示程式碼內容。
- Breadcrumb 永遠顯示目前所在層級（例如「系統 › 訂單出貨 › 結帳」），可直接跳回任一層。
- 所有層級共用一致的 Evidence References。

「專案背景」的地圖另有等價的文字清單（領域與功能、PR 影響的項目、地圖未畫出的連線），讓鍵盤與螢幕報讀器也能完成同樣的操作。

### Entry Experience

1. **PR 摘要首屏**（預設）：PR 標題與「前往 GitHub」（GitHub PR）、一行相關背景（`system.summary`）與「更多背景」、這次修改（`purpose`）、一張主要差異卡（`behavior_changes` 的修改前／修改後）、主要動作「查看這次修改」，以及分開的「介面變動 N 項／待確認事項 N 項」與「查看審查重點」。只沿用既有欄位與來源，前端不產生新主張。多項差異時提供可見的選擇，並說明順序依分析結果、不代表重要性。首屏下方是建議閱讀路徑（原導覽，見 §9）與分析範圍。
2. **System Overview**（專案背景）：一段話說明系統做什麼，加上業務領域地圖（只有領域層）。
3. **PR Overlay**（專案背景切換「PR 疊加」）：只顯示受影響的業務能力，以及每個受影響能力關係最強的至多 2 個鄰居（不含共用基礎），其餘收合成依領域分組的摘要節點（例如「會計財務：12 個能力，未受影響」）；一般 PR 目標 ≤ 15 個節點、每個節點最多畫 2 條線；附上修改目的與業務行為差異。

### 分析狀態與可用路徑

每種狀態都有對應的呈現，主要按鈕一律連到這份結果裡確實存在的內容：

- **完整結果**：摘要、前後差異、相關流程與審查入口。
- **業務說明產生中**（`semantic.status = pending`）：先顯示 PR 描述與靜態分析已辨識的修改，標示 AI 說明尚未完成；回填時不重置入口、選取、展開與捲動位置。
- **業務說明不可用**（`skipped` / `unavailable`）：顯示修改範圍、介面與靜態資訊，附上文件記錄的原因，不拼湊業務差異。
- **沒有可辨識流程**：仍可從相關檔案、功能與介面變動查看。
- **局部失敗**（`semantic.status = failed`）：保留已完成的結果，指出缺少哪些內容與錯誤訊息，提供重新分析（GitHub PR）或回首頁重新分析。
- **完全失敗**（分析沒有產生任何文件）：說明失敗原因與失敗的步驟，保留輸入並提供「重試」與「修改輸入」。
- **PR 已更新**：標示目前的分析版本（head sha），提供「分析新版」；新結果開在新頁面，不混用版本。

沒有發現項目時一律顯示「本次分析未發現…」並附分析範圍與未完成項目，不宣稱沒有風險，也不提供無依據的百分比或等待時間。

### 變更與影響

以具體變更卡作為入口，依序是：`behavior_changes`（依分析結果的順序，不代表重要性）、沒有對應行為差異的流程變動（只列步驟的新增／移除／改變數，不描述業務差異）、沒有業務流程的技術性修改（依功能分組的檔案與介面變動）。每張卡先顯示修改前／修改後；沒有修改前說明時，只有在相關流程全部是新增時才寫「不存在」，否則寫「無法判定」。

- **查看〈流程〉**：流程在卡片下方原地展開，先寫「為什麼相關」（只用資料支持的理由：行為差異列出這條流程、流程步驟有變動、影響分析的狀態），再沿用 3–7 段主軸與每段最多 7 步。播放動畫與流程地圖是選用的。
- **查看相關功能**：局部依賴關係圖，聚焦這張卡涉及的功能與必要鄰居，以約 5–7 個節點為目標；其餘鄰居分組並保留總數、審查提示數、分批與展開入口。違反架構規則的依賴一定畫出，有審查提示的鄰居優先。每條線是一句關係句（例如「訂單呼叫付款」），並附等價的文字清單。
- 依賴圖說明誰依賴誰，流程說明可辨識的步驟順序；兩者都不是執行紀錄。`Impact` 沒有完整的因果鏈，因此影響只寫資料支持的程度（例如「有已確認的程式依賴；是否影響業務行為需確認」），不把靜態依賴寫成確定的業務影響。
- 其他相關流程（影響分析列出但沒有對應變更卡的）直接列出並附理由；與這次修改沒有已知關係的流程收在「其他流程」入口後面。

### Change Visualization

預設突出 Changed 與 Potentially Impacted 的業務能力與流程。

支援 Added、Modified、Removed、Unchanged、Dependency、Potential Impact 等狀態。

Findings 使用獨立圖層，預設不展開。

### 每頁一個焦點問題

採 concept-map 版面：每個畫面最上方寫出它要回答的一個問題（例如「這個 PR 讓結帳流程有什麼不同？」），畫面上只放回答這個問題需要的資訊。

**來源與確認程度分開標示：**

- **來源**（從哪裡來）：AI 說明、PR 描述、靜態分析。以低干擾的文字附在內容後面；混合來源時逐段標示。
- **判讀狀態**（確認到什麼程度）：已確認的結構事實、待確認、無法判定。每張卡只有一個主要狀態，依資料本身的 `status`、相容性或證據狀態決定，不因來源是靜態分析就稱為已確認；狀態同時以符號（✓ ? –）與文字表示，不只靠顏色。

### Flow 呈現（主軸）

大型專案的流程動輒數十步，預設不逐步顯示：

- **主軸**：流程由左到右分成 3–7 段。有 Claude 時，每段是一個業務階段（例如「驗證 → 計算 → 寫入 → 通知」）；沒有 Claude 時，每段是一個能力（同一能力內的連續步驟合併）。
- **主軸上下兩排**：上排放這一段「改了什麼、有什麼風險」，下排放「怎麼做、證據」。
- **隱藏基礎步驟**：落在共用基礎與工程與工具類能力的步驟（權限檢查、記錄、工具函式）預設隱藏。
- **聚焦 PR**：只展開被改到的段落與前後各一段，其餘收合成摘要（例如「前面 4 段」）。
- **一次只展開一段**，每段最多顯示 7 個步驟。

### Before / After

以業務流程為單位比較：

- 預設 Overlay（同一條流程上標出新增、移除、改變的步驟）
- Before / After 切換
- Side-by-Side Comparison
- 適用時播放業務步驟動畫

相同節點在不同版本中應維持穩定的相對位置。

### 程式碼位置呈現

不提供內建程式碼檢視器，也不提供獨立的 Evidence 分頁或圖層（2026-11-11 owner 決定：對 PR review 沒有幫助）。

- 證據資料仍保留在 Artifact 內，用於證據檢查：沒有證據的說明一律丟棄。
- 畫面上只在 Findings 與架構一致性檢查中顯示「檔案路徑:行號」，指向 PR 改動的位置、規則出處與既有的正確做法。這些位置可以複製；GitHub PR 另外提供連到 GitHub 對應行的連結。
- 每格內容只顯示來源與判讀狀態（見「每頁一個焦點問題」），不顯示證據 ID。

## 5. Analysis Architecture

### Layer 1 — Source Acquisition

使用 Git / gh CLI，建立隔離的 Repository Cache 與 Worktree。

分析 PR 的 Merge Base 與 Head，所有結果綁定確切 Commit SHA。

不修改使用者原有的工作目錄。

### Layer 2 — General Static Analysis

分析：

- Changed Files / Symbols
- Imports
- Module Dependencies
- Source References
- Structural Differences

不支援深度分析的語言仍可使用通用結果。

### Layer 3 — Language-Specific Analysis

目的是取得結構事實，作為 System Map 與業務說明的證據，不直接呈現給使用者。

**Python / Django**

分析 Apps、Class、Function、Call Relationships，以及可靜態辨識的 URL Routing、Views、Service、ORM Models、Tasks、Management Commands 等關係。

**TypeScript / JavaScript**

分析 Module、Imports、Functions、Classes、可解析的 Call Relationships、Routes 與 Component Dependencies。

動態呼叫、Reflection 及無法解析的 Framework Behavior 必須標示為 Unknown。

### Layer 4 — System Map Construction

對整個 Repository（Base commit）建立系統結構，依 Repository + Commit 快取，多個 PR 共用。

- **Business Capability 候選**：由 Django Apps、頂層 Package、TS Feature 目錄等結構分組產生
- **拆分過大的能力**：一個能力的入口超過 50 個時，依它的直接子目錄拆成子能力，只拆一層；入口太少的子目錄合併成「其他」，每個上層能力最多 12 個子能力（2026-11-11 修訂：用一個大型 Django monorepo 實測，遞迴拆分會產生過多、過細的業務能力）
- **介面**：偵測外圈與中圈介面（見 §8 架構一致性），並依 `.pr-explorer.yml` 的宣告校正
- **能力類別**：每個能力標為 business（業務）、shared（共用基礎：被多數能力依賴、本身幾乎沒有入口）或 engineering（測試、部署、建置設定、文件、腳本等目錄）
- **Entry Points**：URL Routes、API Endpoints、Background Tasks、Management Commands、排程等對外入口
- **Core Entities**：ORM Models 與主要資料結構
- **Capability 之間的關係**：由 Imports 與 Call Relationships 彙總而來

能力的分組、拆分、類別與關係由靜態分析決定；Capability 的業務名稱與說明、以及把能力歸類到業務領域，由 Layer 7 負責。

### Layer 5 — Business Flow Discovery

- 以 Entry Points 為起點，沿 Call Graph 找出經過的 Capabilities 與 Entities
- 與 PR 修改相關的流程優先
- 將流程歸納為業務步驟（每一步對應一個 Capability 或 Entity 上的動作）
- 使用 Framework-specific Patterns

不得把未確認的依賴當成實際執行路徑。

### Layer 6 — Impact Analysis

採用 Evidence-Gated Impact Analysis，以業務能力與流程為單位呈現。

區分：

- Confirmed Dependency
- Potential Impact
- Evidence-Supported Impact
- Unknown

Impact Propagation 使用有界的依賴探索，避免無限制擴張。

### Layer 7 — Claude Semantic Analysis（核心）

Claude 負責：

- 說明系統的用途
- 把既有的 business 類能力歸類到 6–8 個業務領域，並為領域命名與說明。Claude 只能把能力放進領域，不能拆開、合併或新增能力；每個能力必須且只能屬於一個領域。畫面上標示「由 Claude 歸類」。沒有 Claude 時，以頂層目錄作為領域；若單一目錄裝了超過一半的業務能力（例如把所有應用放在同一個目錄底下的 monorepo），改用它的下一層子目錄，太小的合併，最多 10 個領域
- 為 Business Capability 命名並說明其職責
- 以業務語言描述 Business Flow 的每個步驟
- 把每條 Business Flow 的步驟分成 3–7 個連續的業務階段並命名。只能切分既有步驟的順序，每一步必須且只能屬於一個階段；畫面標示「由 Claude 分段」。沒有 Claude 時以能力作為分段
- 提出有證據支持的架構一致性疑點（一律標示為「需要確認」）
- 解釋 PR Purpose
- 以業務語言說明 Behavior Changes（修改前 / 修改後）
- 整理 Design Rationale
- 以業務影響的角度提出需要人工確認的問題
Claude 不負責產生 Graph Relationships、能力的分組或拆分、Flow 結構、Animation Timeline 或 UI Components；除了把能力歸類到業務領域、把流程步驟分成業務階段之外，它只為靜態分析已建立的結構撰寫說明。

Claude 回覆格式錯誤時自動重試一次；仍失敗才標示為失敗，靜態結果不受影響。

**證據要求：** Claude 的每一句說明都必須引用靜態分析產生的節點或證據 ID；引用不存在或無法對應的說明一律丟棄。Claude 提出的疑點一律標示為「需要確認」。

**沒有 Claude 時：** 仍呈現 System Map 的結構（以技術名稱命名的分組、入口、資料、流程），並清楚標示「業務說明不可用」。

採用 **Adaptive Invocation**：

- System Map：每個 Repository + Commit 只撰寫一次，結果快取重用
- PR 說明：Rules + Complexity Score 決定 Light / Standard / Deep
- 行為、API、資料模型或重要架構變更優先觸發；文件、純格式修改可跳過。跳過時，PR Purpose 直接使用 PR 標題與描述，並標示「未經 Claude 說明」
- 使用者可以手動 Override

知識來源優先順序：Repository 文件（README、docs）→ PR 內容 → 直接關聯的 Jira；只有必要時才擴大搜尋 Slack 或其他知識來源。

## 6. Visualization Data Model

採用三層獨立模型：

**System Map**（Repository + Commit 層級，可跨 PR 重用）
- System Summary
- Business Domains（名稱、說明、所屬能力；註明歸類來源）
- Business Capabilities（名稱、說明、類別 business / shared / engineering、上層能力（拆分時）、包含的技術元件）
- Entry Points
- Core Entities
- Capability Relationships
- Business Flows（業務步驟序列，以及由 Claude 切分的業務階段）
- Architecture Rules（Documented / Inferred，含出處）
- Evidence References

**PR Overlay**
- PR Purpose
- Changed / Impacted Capabilities 與 Flows
- Behavior Changes（修改前 / 修改後，以業務語言描述）
- Impact Status
- Review Findings
- Architecture Checks（每條檢查對應的規則、判定、違反的依賴）
- Evidence References

**Business Flow Timeline**（System Map 中每條 Business Flow 在 Base、Head 各有一條）
- Ordered Business Steps
- 每一步對應的 Capability / Entity
- 每一步的 Step Change Status（Added / Removed / Changed / Unchanged，由 Base 與 Head 比對得出）
- Supported Branches
- Evidence Status

所有模型共用穩定 ID 與 Evidence References。Evidence 指向「檔案:行號」與產生它的分析器。

使用版本化 JSON Schema 驗證 Artifact，並保存分析器及 Schema 版本，以支援快取失效與未來資料升級。

## 7. Animation Engine

採用 **Evidence-Gated Animation**，粒度為業務步驟。

- 使用預先分析的 Business Flow Timeline
- 自動播放、暫停、重播及速度控制
- 每一步 Highlight 對應的 Capability / Entity，並在說明 Panel 顯示這一步的業務說明
- 對未確認的條件分支標示可能路徑
- 不推測未知的 Runtime State
- 不宣稱動畫是實際 Runtime Trace

若缺乏足夠的執行順序證據，改用靜態 Flow Diagram，不強制產生動畫。

## 8. Review Findings & Architecture Consistency

採用 **Evidence-Gated Findings**：

- Static Analyzer 產生可驗證的技術 Findings，並說明它影響哪個業務能力或流程
- Claude 可以提出有證據支持的業務層面疑點
- 兩類 Finding 明確分開
- 每個 Finding 都必須關聯 PR 變更
- 必須提供 Evidence（檔案:行號）
- 無法確認的疑點不可標示成已證實的 Bug

### Architecture Consistency（架構一致性）

檢查 PR 是否符合系統既有的**軟體架構**（不是程式碼風格），放在 PR 視角的主要區塊。

**核心原則（2026-11-11 owner 定義）：以介面為出發點。越外層的介面越靠近使用者或客戶端，越應該少改；改動應盡量留在內層。**

**介面圈別：**

- **外圈（客戶端直接使用）**：對外 HTTP API（路由、請求與回應欄位）、Web App 的頁面與路由、被發佈的 SDK 或套件的公開介面、送往外部的格式（Webhook、EDI、通知內容）。
- **中圈（系統內各能力之間）**：能力之間的對外函式、背景任務的參數、內部事件、import-linter 等模組契約。
- **內圈（能力內部實作）**：不列為介面；改動集中在這裡是最理想的狀態。

**外圈的判定：** 先自動偵測（`/api` 路由與 DRF Serializer 欄位、前端 App 路由、有發佈設定的套件、對外送出的格式），Repository 也可以用設定檔 `.pr-explorer.yml` 明確宣告或排除外圈介面；設定檔優先於自動偵測。

**介面變動的相容性：**

- **新增**（例如新的路由、回應多一個選填欄位）：相容。
- **修改或移除**（例如欄位改名或刪除、路由移除、新增必填參數、回應格式改變）：可能破壞客戶端，標示為警告。
- 回傳檔案的 API（CSV、Excel 匯出）：以欄位定義（欄位鍵）比對，新增欄位為相容，移除或改名為可能破壞（例如下游的試算表或 BI 匯入）。
- 靜態分析能確定的變動標示「已驗證」；無法確定相容性時標示「無法判定」，並寫明是哪個定義檔有改動。

**呈現順序：** PR 視角的架構一致性區塊依圈別由外往內排列，外圈變動放最上面，並列出需要確認的事：這個改動是否必要、能否改在更內層、是否需要版本控管或通知客戶端。地圖上以圈別標示每個能力被 PR 動到的最外層介面。

以下的依賴規則檢查屬於中圈，排在介面變動之後。

**規則來源：**

- **Documented Rule**：Repository 內已寫明的架構規則。優先讀取可機器檢查的設定（例如 `.importlinter.ini`：clean architecture 分層、模組獨立、禁止依賴、legacy 隔離等契約），其次是架構文件（ADR、`docs/`、README）。
- **Inferred Rule**：從整個系統的依賴統計歸納，例如「其他能力呼叫會計時，幾乎都透過會計的對外介面」。必須附上統計數字與例子。
- **Unverified Hypothesis**：Claude 提出、尚未有規則或統計支持的疑點，一律標示為「需要確認」。

**檢查項目：**

- **依賴方向**：PR 新增或改變的依賴是否違反分層、禁止依賴或模組獨立規則；是否形成新的循環依賴。
- **對外介面**：跨能力的呼叫是否透過既有的對外介面，而不是直接使用對方的內部模組或資料表。
- **放置位置**：新的模組、資料或入口是否放在與同類既有元素一致的能力中。
- **共用機制**：背景工作、通知、權限、規則引擎等，是否使用系統既有的共用模組，而不是另起一套。
- **架構債**：PR 在規則例外清單中新增例外（例如 import-linter 的 ignore 清單）時，標示為「新增架構債」。

**判定：** 符合、違反、新增架構債、無法判定。Documented Rule 與 Inferred Rule 的違反由靜態分析判定並標示「已驗證」；Unverified Hypothesis 只能是「需要確認」。

**呈現：** 地圖上把違反規則的依賴線標成醒目顏色；審查卡列出規則、規則出處（「檔案:行號」）、這次 PR 的哪個改動違反、以及既有的正確做法範例。

**審查重點：** 分為對外介面變動、架構一致性、待確認事項（Findings）三類，各自計數，不加總成風險總數，也不把 Finding 的 severity 與介面的相容性換算成共同分數（severity 只在待確認事項內比較）。每張卡先寫「需確認」什麼，再列涉及的功能、已知事實與不確定之處，以及「檔案:行號」與 GitHub 連結。待確認事項依判讀狀態分組，不依來源分組。只有明確的關聯（同一條架構規則 `rule_id`，或同一個程式位置）才標示「相關檢查」，不做語意去重。

## 9. AI Visual Copilot

MVP 採用 **Pre-generated Guided Explorer**。

Claude 在分析階段產生解說文字與導覽內容，使用者探索時不需要再次呼叫 Claude。

預設導覽順序：

1. 這個系統是做什麼的
2. 系統由哪些業務能力組成
3. 與這次修改相關的業務流程
4. 這個 PR 改變了什麼業務行為
5. 可能受影響的其他業務功能
6. 需要確認的問題

支援：

- 預先建立的 Adaptive Walkthrough
- Optional Learning Coach
- 導覽以「建議閱讀路徑」的形式放在 PR 摘要首屏下方，可跳著看，不是必走的教學流程
- 點擊導覽步驟或問題時，只在原地展開或收合說明，不切換頁面、不改變地圖的焦點
- 展開後提供「看…」按鈕（看專案背景、看這條流程、看審查重點等）；使用者按下才切換到對應的主導覽入口

不支援自由生成新問題的 AI 回答；超出 Artifact 範圍時應明確告知資料不足。

## 10. Recommended Technical Stack

| Component | Recommendation |
|---|---|
| Frontend | React + TypeScript |
| Architecture Graph | React Flow + Custom SVG Nodes |
| Automatic Layout | ELK.js |
| Backend | Python + FastAPI |
| Python Analysis | Python AST + Django-aware Rules |
| TypeScript Analysis | TypeScript Compiler API |
| Semantic Analysis | Claude Code CLI |
| GitHub | Git + gh CLI |
| Knowledge Sources | Configured MCP Tools |
| Database | SQLite |
| Artifact Storage | Versioned Local JSON Files |

以上為技術預設；若遇到套件限制，允許使用等效實作，不改變功能與資料介面。

## 11. Local Security

- Backend 預設監聽 `0.0.0.0`（2026-11-11 owner 決定；`--host 127.0.0.1` 可限本機）
- 不需要應用程式登入
- 使用本機既有 GitHub / MCP 授權
- 不執行 PR 中的程式碼
- 不允許 Repository 內容作為 Claude 的系統指令
- Claude 工具權限限制為完成分析所需的讀取操作
- 不將本機 Credentials 或 Secrets 寫入 Artifact
- 產生內容僅存放本機
- 保留 Artifact 與 Evidence 的來源資訊

## 12. Implementation Milestones

### M1 — System Map + PR Overlay Vertical Slice

以一個真實的 Python / Django Repository 與 PR 完成：

PR URL → Clone → Static Analysis → System Map（含 Claude 業務說明）→ PR Overlay → 至少一條與 PR 相關的業務流程動畫。

先驗證不熟系統的人能從畫面看懂「系統是什麼、PR 動到哪裡」。

### M2 — Business Flow & Evidence Quality

完成完整的 Business Flow Explorer（所有流程可瀏覽、聚焦）、多層級展開、Before / After、Impact Analysis、Findings 圖層、Architecture Consistency 與穩定自動布局。

### M3 — Generalization & Validation

加入 TypeScript / JavaScript 深度分析，以及其他語言的通用分析。

使用 5–10 個真實 PR 驗證分析正確性，並讓不熟悉專案的工程師參與理解測試。

## 13. Definition of Done

### Functional

- 可以從真實 GitHub PR URL 完成分析
- 能展示整個 Repository 的 System Map，且同一個 Repository + Commit 只建立一次
- 大型 Repository（例如數萬個檔案的 monorepo）的每一個畫面都不超過節點上限；System Overview 只顯示業務領域，工程與工具類能力預設不出現
- 流程預設以 3–7 段主軸呈現，展開的段落每段不超過 7 個步驟
- PR 動到的介面依圈別（外圈 → 中圈）列出，外圈的修改與移除標示為可能破壞客戶端
- 對有 import-linter 設定的 Repository，PR 新增的違規依賴會以「已驗證」的架構一致性問題呈現，並附規則出處
- PR Overlay 能在 System Map 上標出受影響的業務能力與流程
- Before / After 與正確 Commit 對應
- Business Flow Explorer 可聚焦相關流程
- 業務步驟動畫與說明 Panel 正確同步
- Evidence / Unknown 狀態清楚
- 相同 Commit 可重用分析快取
- PR 更新後能偵測並提示重新分析
- 沒有 Claude 時仍呈現系統結構，並標示業務說明不可用

### Quality

- 每一句業務說明都有可追溯的 Evidence Reference
- 重要架構關係都有可追溯的 Source Reference
- 未驗證的執行路徑不得呈現為確定的 Runtime 行為
- Review Findings 與 PR Changes 有明確關聯
- 不支援的分析能力以 Partial / Unknown 呈現，而不是生成猜測內容

### Understanding Validation

以真實 PR 與預先建立的理解題進行測試，受試者是不熟悉該 Repository 的工程師，測試時不提供原始碼。

評估：

- 系統用途理解正確率
- 主要業務流程理解正確率
- PR Purpose 理解正確率
- Behavior Change（業務層面）理解正確率
- Impact 理解正確率
- 完成理解所需時間
- 使用者是否因不正確的視覺化或說明受到誤導

**建議初始驗收目標：** 核心理解題正確率至少 80%，相較直接閱讀 GitHub PR，理解時間中位數降低至少 25%。這些是待實測驗證的 MVP 目標，不是目前已達成的結果。

## 14. Non-Goals

本專案不是：

- GitHub Code Review 替代品
- AI Code Review Approval Agent
- 程式碼閱讀或逐行追蹤工具
- 自動化測試平台
- Runtime Debugging Tool
- 完整 IDE
- 通用架構圖編輯器

**最終衡量標準：不是產生多少張圖，而是不熟系統的人能否更快、更正確地理解這個系統與這個 PR。**

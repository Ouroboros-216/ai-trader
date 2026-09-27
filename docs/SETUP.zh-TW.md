# 飛機、AI API、MT5 接線流程

## 最快設定方式：直接用視窗

解壓縮新版 `AITrader-v0.8.13.zip`，進入其中的 `ai-trader` 資料夾，**雙擊 `開啟設定.cmd`**。第一次會自動安裝本專案的獨立 Python 環境（不下載套件）；設定視窗開啟後 CMD 會關閉。在視窗填帳號、完整伺服器名稱，選「模擬／實盤」，再選 Gemini 或 OpenAI（GPT），填該供應商的模型 ID／API key、飛機 bot token。按「測試 Gemini」或「測試 OpenAI（GPT）」，向自己的 bot 傳 `/start` 後按「讀取飛機 ID」，從清單選自己的 ID，再按「儲存設定」。通過測試的連線會自動設為 enabled，不需手改 `true`。

金鑰不寫進 `local.json`；視窗會用 Windows 目前使用者的 DPAPI 加密存入 `config/secrets.bin`。這個檔案不在 ZIP 裡，也無法在另一個 Windows 使用者帳戶直接解密。若原本已填好 `local.json`，新版壓縮包不會覆蓋它。

接著依第 6 節把 EA 掛到相應 MT5。設定視窗按「儲存設定」會將帳號、伺服器與帳戶類型綁定資料寫入 MT5 Common Files；新版 EA 的帳號 `0`、伺服器空白代表自動讀取此綁定，並核對目前登入的 MT5 帳戶。若佣金或其他 EA 參數有自訂，仍在 EA「輸入」頁按「載入／Load」選擇 `config/AITrader.generated.set`。按一次「啟動服務」會載入所有已儲存的帳號，選中的帳號還沒掛 EA 也不會阻擋；該帳號會等待連線。之後可用「檢查 MT5」核對目前帳號。策略與自動交易仍要透過飛機／MT5 個別確認。以下手動步驟保留給需要用命令列的情況。

「每手開平合計佣金」可以只填 **一個數值**，供該帳戶所有商品使用；若商品費率不同，可在 EA 進階參數依商品順序設定。單位是帳戶貨幣、每 1 手完整開平的總額。未知時填 `-1`。掛上 EA 後可按「讀取 MT5 佣金」：只有 MT5 券商資料提供可明確換算的簡單固定規則才會建議帶入，帶入後仍要儲存並重新掛 EA。無法換算就維持未知並阻擋新單；可看帳戶實際成交紀錄的佣金或向券商確認。這個版本沒有啟用 AI 網路搜尋工具；公開資料也不能替代帳戶實際費率與貨幣核對。

## 0. 先知道三者怎麼連

```text
你的 Telegram 私人聊天
          ⇅
電腦上的 AI Trader Python 服務 ⇄ 你選擇的 Gemini／OpenAI API
          ⇅ 本機 Common Files
      MT5 的 AITrader EA ⇄ 券商帳戶
```

Telegram 不是券商登入介面；AI API 不拿券商密碼。Python 讀寫本機訊息檔，由 EA 驗證並下單。電腦／VPS、MT5 和 Python 都需運行。此版本不用 DLL，也不用在 MT5 加入 API 的 WebRequest 網址。

建議先用**獨立 MT5 終端及專用 Demo 帳戶**。不要把這支 EA 放到原有黃金研究終端的圖表上，不要與原 EA 共用帳戶。沒有替你建立或變更任何帳戶。

## 1. 安裝程式，先填非機密設定

在 PowerShell 進入本專案；以下為目前電腦的位置，搬到 VPS 後改成新位置：

```powershell
Set-Location 'C:\Users\azsxd\Documents\ChatGPT\AI操盤\ai-trader'
.\scripts\install.ps1
```

如果系統沒有 `python` 指令，而要先用目前研究環境的 Python：

```powershell
.\scripts\install.ps1 -Python '..\.venv\Scripts\python.exe'
```

安裝會建立獨立 `.venv`，並複製 `config/example.json` 成 `config/local.json`；不覆寫已存在的 local.json。

用文字編輯器打開 `config/local.json`，先填：

| 欄位 | 你要填什麼 |
| --- | --- |
| `account` | MT5 帳號，保留雙引號，例如 `"12345678"` |
| `account_mode` | `demo` 或 `real`；舊設定預設為 `demo` |
| `server` | MT5 顯示的完整伺服器名稱，大小寫一致 |
| `magic` | 保持 `26092751`，須與 EA 一致 |
| `bridge_dir` | 先保持預設 `%APPDATA%/MetaQuotes/Terminal/Common/Files/AITrader/demo-1` |
| `provider.kind` | `gemini` 或 `openai`；從視窗選擇更容易 |
| `provider.model` | 所選供應商提供的完整 API 模型 ID |
| `provider.enabled` | 初始保持 `false`，連線測試成功後改成 `true` |
| `telegram.enabled` | 初始保持 `false`，配對完成後改成 `true` |

**JSON 不能有註解或尾逗號。** 這個設定檔只填帳號和非機密參數，不填 API key／bot token。

## 2. 取得 Gemini API key 與模型 ID

1. 打開 [Google AI Studio](https://aistudio.google.com/)，使用自己的 Google 帳戶。
2. 到 API Keys 頁建立或選取 API key；依官方流程選擇所屬專案。[Google 官方 key 說明](https://ai.google.dev/gemini-api/docs/api-key)
3. 在該專案確認模型可用性與免費層配額，把**API 模型 ID**填入 `provider.model`，不要只填介面上的顯示名稱。
4. 想先用免費層，需在 Google 帳戶／專案側確認計費狀態。程式不會開通付費，但無法替已綁定計費的 key 保證零費用。額度依專案／模型而異，不是固定每小時 24 次。[官方 rate limits](https://ai.google.dev/gemini-api/docs/rate-limits)

先把 key 留在你本機的密碼管理工具，下一節一起輸入。不需要傳给 Codex 或 Telegram。

### 改用 OpenAI（GPT）API

在 [OpenAI API 平台](https://platform.openai.com/api-keys) 建立 API key，並確認所選模型可供你的 API 專案使用；**ChatGPT 訂閱與 API 用量分開**。在設定視窗「② AI API」選「OpenAI（GPT）」，填入平台提供的完整模型 ID 和 OpenAI API key，按「測試 OpenAI（GPT）」成功後按「儲存設定」，最後停止舊服務並重新啟動。所有帳號共用這項選擇；原 Gemini key 仍加密保存，切回 Gemini 時可沿用。測試與後續分析會產生 API 用量，程式不會在 Gemini 503 時自行切換至 OpenAI。

這個接法使用 OpenAI [Responses API](https://developers.openai.com/api/docs/guides/text) 的 JSON 回覆模式，關閉伺服端回應儲存，不開啟網路搜尋或其他工具。模型仍只能提出策略與交易決策；EA 的帳號核對與風控不變。OpenAI 回應逾時、限流、JSON 無效或不完整時，本輪不產生新單。其他 AI 供應商尚未接入，需要新增對應 adapter；「可更換」不代表任意 API key 可以直接填進同一欄使用。

如果測試顯示 `OpenAI HTTP 400`，v0.8.9 起會附上 API 的安全 `參數`／`代碼`，不顯示伺服器原始錯誤文字、金鑰或行情內容。`gpt-5.4` 是有效的 [OpenAI 模型 ID](https://developers.openai.com/api/docs/models/gpt-5.4)；400 仍可能是模型或某個請求參數不被帳戶接受。若 API 明確指出 `text.format` 不受支援，程式會再用一般文字模式請求一次，仍要通過 JSON 與交易決策驗證；其他 400 不會盲目重試。

v0.8.10 把 OpenAI `input` 從單一 JSON 字串改成官方 Responses API 的明確使用者訊息格式（`input_text`）。這是針對某些服務回報 `參數=input` 的相容修正。若按設定視窗「測試 OpenAI（GPT）」時仍是 400，視窗會顯示已遮蔽金鑰、截短的伺服器原因，方便辨認；一般交易分析仍不會把原始錯誤傳到飛機或寫進交易紀錄。分享畫面前仍請確認沒有露出 API key 欄位。

OpenAI v0.8.11 修正 JSON 模式的請求：`input` 訊息明確包含 `JSON` 字樣，符合 API 的格式驗證。

OpenAI v0.8.12 讓「自動模式」策略草案最多等待 120 秒（`strategy_timeout_seconds`，可設 30–300 秒）；測試連線與持倉決策仍使用原本的 `timeout_seconds`。逾時不套用結果，也不自動重送；系統會暫停相同模型的新呼叫約 2 分鐘。請注意：請求可能已到達 API 並消耗 token，即使本機沒有取得可用回覆。用量以 OpenAI 平台顯示為準。

v0.8.13 起，飛機上的策略草案以中文段落顯示商品、方向、進場、失效、管理、術語及風險，不再直接印出原始 JSON。收到草案後可以直接回覆問題，AI 會收到這份尚未套用的草案作為討論背景；例如傳「這張草案的 BOS 是什麼？」。要改條件則傳「修改 只做空」（也可用「調整」或「改成」），系統會產生新的待確認草案，舊提案失效。聊天本身不會確認或套用策略，也會使用一次 AI API 呼叫；確認策略後仍須另行確認「啟動」。

## 3. 建立 Telegram bot

1. Telegram 開啟官方 [@BotFather](https://t.me/BotFather)。
2. 傳送 `/newbot`，依指示設定名稱與以 `bot` 結尾的 username。
3. 保存 BotFather 給你的 **bot token**。這是 bot 的憑證，不是 Telegram 使用者 ID。[Telegram 官方建立流程](https://core.telegram.org/bots/tutorial#obtain-your-bot-token)
4. 打開剛建立的 bot，按 Start 或傳送 `/start`。此時還沒有服務運行，不回覆是正常的。

使用專屬 bot，不要與其他正在 `getUpdates` 輪詢的程式共用，也不要對這個 bot 設 webhook。

## 4. 在本機輸入兩個憑證

在剛才的 PowerShell 執行：

```powershell
.\scripts\set-secrets.ps1
```

會依序以隱藏輸入詢問 Gemini key、OpenAI key、Telegram token。預設存入**目前 Windows 使用者環境變數**，也設在目前 PowerShell 程序；不寫入專案、Git 或日誌。

環境變數名稱：`GEMINI_API_KEY`、`AI_TRADER_TELEGRAM_TOKEN`。若只想本次 PowerShell 有效，加 `-SessionOnly`。已開啟的服務需重啟才能取得新憑證。

接著測試 Gemini（會消耗一次 API 配額，若使用付費 key 可能計費）：

```powershell
.\.venv\Scripts\python.exe -m aitrader.cli --config .\config\local.json api-check
```

成功時輸出包含「連線成功」的 JSON。成功後將 `provider.enabled` 改為 `true`。

## 5. 取得自己的 Telegram ID 並配對

先確認已對自己的 bot 傳 `/start`，再執行：

```powershell
.\.venv\Scripts\python.exe -m aitrader.cli --config .\config\local.json telegram-info
```

你會看到 bot username，以及 `private_user_chat_ids`，例如：

```json
{"bot_username":"your_demo_bot","private_user_chat_ids":{"123456789":123456789}}
```

將你自己的數字填進 local.json：

```json
"telegram": {
  "enabled": true,
  "token_env": "AI_TRADER_TELEGRAM_TOKEN",
  "user_id": 123456789,
  "chat_id": 123456789
}
```

兩個 ID 都是數字，不是 `@username`，也不是 bot ID。本版只接受這個使用者的私人聊天；其他人、群組和超過兩分鐘的舊文字命令都忽略。

若清單為空，重新傳 `/start` 後再查。若多個 ID，自己核對，工具不會自動配對第一個人。**服務啟動後不要再執行 telegram-info**，避免與正式輪詢互搶訊息。

## 6. 安裝 EA 到獨立 MT5

1. 登入準備好的 MT5 終端，核對帳號／伺服器／帳戶類型與設定視窗一致。
2. 在 MT5 選「檔案 → 開啟資料夾」，複製開啟的完整路徑。
3. 交付包已含編譯好的 EA；若自己修改原始碼，先執行 `.\scripts\compile.ps1`。
4. 將下列參數替換成剛複製的資料夾：

```powershell
.\scripts\deploy.ps1 -TerminalDataDirectory 'C:\你實際的MT5資料夾'
```

也可手動將 `mql5/AITrader.ex5` 複製到該终端的 `MQL5/Experts/AITrader/`。

5. 在 MT5 導航器的 EA 清單重新整理，把 `AITrader` 拖到**一張圖表**。它會依確認的策略管理多商品，不要每個商品都掛一次。先在設定視窗按「儲存設定」；帳號 `0`、伺服器空白時 EA 自動讀取已儲存的綁定並核對帳號與帳戶類型。自訂 EA 商品／成本參數時，在「輸入」頁按「載入／Load」選 `config/AITrader.generated.set`。
6. 設定 EA 輸入：

| EA 欄位 | 設定 |
| --- | --- |
| `InpDemoLogin` | `0` 代表自動讀取已儲存的帳戶綁定；手動填值也必須與綁定一致 |
| `InpDemoServer` | 空白代表自動讀取已儲存的伺服器；手動填值也必須與綁定一致 |
| `InpBridge` | 保持 `AITrader\demo-1`；對應 Python bridge_dir 最後兩層 |
| `InpMagic` | `26092751` |
| `InpSymbols` | 保持 `AUTO`；EA 只分析你在 MT5「市場報價」手動顯示的商品，最多 10 個；後綴有歧義會先詢問 |
| `InpCommissionRoundTurn` | 填一個共用值，或依商品順序各填每 1 手開平合計佣金；單位為帳戶貨幣。未知保持 `-1`，且 MT5 無可換算規則時會阻擋交易 |
| `InpMaxSpreadPoints` | 依商品順序填允許最大點差，單位為該商品 point；預設共用值 `25` 只是工程起點 |
| `InpSlippagePoints` | 依商品順序填單邊滑價預算 points，預設共用值 `3` |
| `InpBars` | 預設每週期 100 根已完成 K 棒 |

例如某商品每手單邊佣金 3.5，round turn 應填 7；真正零佣金商品才能填 0。不要把小手數的整筆佣金誤填為每手佣金。三個商品可填一個共用值，也可填三個各自對應的值。

7. 開啟 MT5「演算法交易／Algo Trading」，並允許此 EA 的交易權限。
8. 面板應出現 `DEMO ACCOUNT` 或 `REAL ACCOUNT`、正確帳號、`EA state=true`。沒有策略／服務時保持暫停。

面板底部有文字輸入框和「送出訊息」。服務啟動後可在 MT5 直接輸入 `狀態`、一般問題，或 `策略 你的規則`；回覆顯示在面板，長內容可按「下頁」。每則最多 1,000 字。從面板輸入 `啟動`、`平倉`、`重設回撤` 或策略修改仍需檢閱並確認提案；對話不會跳過既有風控。Telegram 的 `狀態`／`持倉` 也改為易讀的中文摘要。

程式接受設定視窗指定的模擬或實盤類型；競賽帳戶、帳號或伺服器不一致都拒絕初始化。策略商品後綴有多個候選時，會列出選項，請指定精確名稱。

## 7. 核對 MT5 通訊，再啟動服務

### 同一台電腦管理多個帳號

先停止舊服務，再以 v0.8.4 ZIP 覆蓋程式檔；保留既有 `config/local.json`、`config/secrets.bin` 及 `runtime` 資料夾。首次在新版設定視窗按「儲存設定」，原帳號會成為第一個獨立帳號，其策略與資料庫維持原位，且仍是模擬模式。視窗下方按「新增帳號」，填第二個 MT5 登入、完整伺服器、帳戶類型與該帳號佣金，再按「儲存設定」。可重複加入更多帳號；既有帳號請用下拉清單切換，勿直接把舊帳號欄位改成新登入。

每個帳號開一個 MT5 終端機／視窗，登入相應帳號，在各自圖表掛 **v1.010** EA。EA 的 `InpBridge=AITrader\demo-1`、`InpDemoLogin=0`、`InpDemoServer` 空白可保持預設；它從 Common Files 的 `AITrader/accounts.txt` 按當前登入帳號、伺服器與帳戶類型自動選通訊目錄和成本。若是從舊 `.set` 載入明確帳號／伺服器，請將這三欄恢復預設再重掛。每個帳號各有策略、暫停、持倉與風控鎖；服務只啟動一份，所選 AI API 與飛機 bot 共用。

Telegram 傳 `帳號` 列出清單，直接傳帳號數字（例如 `53070196`）即可選定帳號；若不同伺服器恰有相同帳號數字，才須傳完整 `demo-...` 代碼。之後的 `狀態`、`策略`、`啟動`、`平倉` 只作用於該帳號。提案按鈕含帳號綁定，切換帳號後仍會確認原帳號的提案。新增帳號或變更共用 API 設定後，停止並重啟服務才會載入。風控上限按各帳戶個別計算，沒有跨帳戶合併保證金或風險預算。此版只支援同一台 Windows、同一使用者下的多個 MT5 終端機；跨電腦帳號需另行部署獨立服務。

飛機也可直接傳 `帳號` 或 `/start`，從中文按鈕選帳號；選取後會出現「狀態、持倉、原因、策略、暫停、啟動、平倉、重設回撤、返回帳號清單」按鈕。按「策略」會提示如何輸入策略文字；啟動、平倉及重設仍需檢閱提案並另按確認。按「返回帳號清單」會退出對話所選帳號，此後指令不再自動作用於前一帳號；它不會停止服務，也不會改變前一帳號的交易啟停狀態。飛機的 `/` 指令選單會在配對私人聊天顯示中文說明；指令名稱依平台格式保留英文。

從 v0.8.7 起，按設定視窗「檢查更新」會把已編譯的 `AITrader.ex5` 覆蓋到本機每個已安裝 AITrader EA 的 MT5 終端機資料夾 `MQL5/Experts/AITrader/`。更新後仍須在各終端機重新掛載 EA（或重開 MT5），讓新版檔案載入；在 EA 回報 v1.010 前，服務禁止新單。從未安裝過 EA 的終端機不會被自動建立資料夾，須依上方安裝步驟先裝一次。

設定視窗下方「券商／帳號」清單顯示券商伺服器名稱與帳號，例如 `ICMarketsSC-Demo｜53070196`；實盤會標「實盤」。內部帳號代碼不需手動辨認。

### 移除不再管理的帳號

先在設定視窗按「停止服務」，確認所有帳號服務已退出，再從欲移除帳號的 MT5 圖表卸下 AITrader EA。該帳號須無本系統持倉、無待執行指令；有既有策略時，EA 最後一次平倉快照須在五分鐘內。到設定視窗下方的「券商／帳號」下拉清單選此帳號，按「移除帳號」並確認。移除後重新啟動服務，飛機帳號清單會更新。至少保留一個帳號。此操作保留原設定檔和 SQLite 交易紀錄，不會代替平倉；若快照過期，重新掛 EA 檢查持倉，再卸下 EA 後立即移除。

### 同一策略套用全部帳號

飛機傳 `策略全部 用 SMC、只做空、以 M15 進場……`；也可選帳號後按「全部策略」看輸入提示。系統只呼叫一次 AI 產生共用交易規則，會列出每個帳號的實際商品映射、策略版本與單筆／總風險。核對後按「確認套用全部策略」。有既有策略的帳號必須有新鮮 EA 快照且本系統持倉為空；商品映射若標「設定值（待 EA 核對）」，先檢查該帳號 EA 與券商商品名稱。確認成功後所有帳號仍暫停新單；逐一選帳號檢查 `狀態`，再個別 `啟動` 和確認。若批量套用中途異常，全部暫停，應檢查各帳號策略版本後重建草案。

每帳號的即時買賣價差、設定的最大點差、佣金和滑價預算來自該帳號 EA 的快照；最近券商下單／平倉呼叫的耗時與拒單紀錄也只用於該帳號的 AI 判斷。耗時是 EA 端呼叫耗時，不等於保證成交時間或下一筆滑價。佣金未知時 EA 阻擋新單；成本過高或成交不穩時 AI 應觀望，EA 仍會獨立檢查點差、保證金和風險並按該帳號權益計算手數。不同帳號可能因此對相同策略作出不同決定。

如果 Telegram 的 `狀態` 顯示 MT5 快照過期，先傳 `帳號` 並選擇目前 MT5 正在登入的帳號，再確認該帳號 EA 還在圖表上。單一 MT5 視窗切換登入後，前一個帳號的快照會過期；這是正常的安全阻擋。若提示帳戶權益為零，請檢查 Demo 餘額；系統不會在零權益帳戶開新單。

```powershell
.\.venv\Scripts\python.exe -m aitrader.cli --config .\config\local.json check
```

預期顯示 `bridge: ok`、與所選帳戶類型一致的 `demo: true/false`、映射商品清單。EA 每五秒發布快照；週末、休市或行情未下載時商品可能尚未 ready，這時不能確認啟動。

第一次以前景方式運行，方便看啟動狀態：

```powershell
.\scripts\start.ps1 -Foreground
```

然後在 Telegram 傳送 `狀態`。如果能看到帳戶權益、策略與持倉，代表 Telegram → Python → MT5 三段已接通。

## 8. 建立第一個策略並確認啟動

若不想先指定 SMC 等方法，可在飛機或 MT5 面板直接傳 `自動模式`。AI 會根據該帳號 EA 至少有 20 根 M15 與 H1 已完成 K 棒的商品，提出可檢閱的交易方法、商品與進出場條件；休市或佣金未知時也可先擬草案，但啟動交易仍須等待報價與成本就緒。也可傳 `自動模式 每筆風險0.3%`；單獨傳 `風險0.3%` 會走同一草案流程。百分比視為單筆風險，超過目前 0.5% 上限會被拒絕。確認策略卡後仍須另傳 `啟動` 並確認，才會開始新單。之後 AI 定期在策略卡允許的方法內擇優或觀望，不會自行發布新策略版本。

若飛機回覆 `Gemini HTTP 400/401/403/404/429/503`，請依訊息中的固定提示處理；這個代碼不包含 API key。`400` 可能是模型不接受目前請求參數或內容，`401/403` 檢查金鑰與專案權限，`404` 核對模型 ID，`429` 查看 AI Studio 的實際配額，`503` 稍後重試。只見 `HTTPError` 的舊版請先升級服務，才能知道錯誤碼。請勿把 API key 或 bot token 貼到飛機聊天或截圖。

若 AI Studio 用量頁同時顯示成功的 Gemini 3.8 Flash 請求與 `503 ServiceUnavailable`，代表基本金鑰與模型可用；`503` 仍可能因該次請求的服務容量不足而發生。v0.8.4 把自動模式的 K 棒資料壓縮並限制為 M15/H1 各 20 根、H4 12 根；連續 503 會暫停新 API 呼叫 60、120、240 秒等（最多 15 分鐘）。這是減少負擔與無效重試，不保證 Google 端不再回 503；失敗的分析不會產生新的 AI 下單指令。

若 3.8 Flash 持續回 503，可在設定視窗將「模型 ID」改為 `gemini-3.5-flash`，按「測試 Gemini」成功後按「儲存設定」，停止舊服務並等待退出，再按「啟動服務」。金鑰不用重填；模型屬於全帳號共用的 AI API 設定。v0.8.4 的暫時性 503 等待只針對發生錯誤的模型，因此換模型後不會沿用舊模型的 503 等待；429 配額等待仍由同一專案共用。切換模型不保證不再出現 503，也不代表交易績效較好；是否可用免費層以 AI Studio 的專案配額為準。

從 v0.8.4 起，設定視窗有「檢查更新」。按下後會查詢公開 GitHub Release；若有新版，確認一次即會下載 ZIP 與 SHA-256、校驗檔案、停止正在運行的服務、更新程式並重開設定視窗。原 `config/local.json`、`config/secrets.bin`、`runtime` 與 `.venv` 不會被覆蓋；若服務原本在運行，更新後會自動重啟。更新結果寫在 `runtime/update.log`，詳細程序輸出在 `runtime/update.err.log`。請先儲存設定視窗尚未儲存的修改。v0.8.3 尚無此按鈕，需最後一次手動覆蓋 v0.8.4；往後使用按鈕。新版 ZIP 已包含可編譯成功的 EX5 並會自動覆蓋已安裝位置；更新後仍須重新掛載 EA 才會載入新版本。

v0.8.5 起，若 Gemini 已回應但內容無法採用，飛機會區分「輸出 token 上限」、「回答被阻擋」、「空回答」和「JSON 格式錯誤」，本機呼叫紀錄也會記下對應代碼。這些情況都不會產生新下單指令。Gemini 3.5 Flash 的預設思考會消耗部分輸出 token；看到 `max_tokens` 才需考慮調整回應上限，單次 `ValueError` 後下一次成功並不能判定原因。

v0.8.6 起，飛機回覆的帳號標題、錯誤提示、交易回報與帳號清單顯示「券商伺服器｜帳號」，例如 `VantageMarkets-Demo｜26091375`。舊的 `demo-...` 代碼仍可用於選帳號，但只是內部識別，不再顯示在一般回覆標題。

在 Telegram 傳：

```text
策略 用 SMC 分析 XAUUSD 和 EURUSD，以 H1、H4 判斷背景，以 M15 確認進場。只做空；單筆風險 0.5%，總風險 1.5%，不要攤平，條件失效可提前平倉。
```

系統回覆策略卡，包含結構、BOS/FVG 等採用定義、進場與失效條件。檢查內容，按「確認此提案」；也可到 MT5 面板翻頁讀完後按「確認待辦」。提案十分鐘過期，舊提案不能覆蓋新版本。

**確認策略只保存設定，還不開新單。** 接著傳 `啟動`，檢查啟動提案並確認。面板顯示自動交易後，才會開始分析與交易。AI 可以選擇不交易，不需要為了確認系統在動而強迫它進場。

改策略仍以 `策略 ` 開頭；其他自然語言問題只讀回答。首版不能直接用任意聊天句子跳過操作確認。

## 9. 日常操作與停止

| 操作 | 效果 |
| --- | --- |
| `狀態`／`持倉` | 讀取 MT5 快照 |
| `原因` | 查看最近決策／執行紀錄，不呼叫 AI |
| `為什麼這筆交易提早出場？` | AI 依快照與記錄解釋，使用 API |
| `暫停` | 停止新單，既有倉位可繼續由 AI 管理，EA 保護持續 |
| `平倉`＋確認 | 暫停新單並平掉提出時列出的本系統持倉 |
| `重設回撤`＋確認 | 無本系統持倉時重設總回撤基準，保持暫停 |

前景服務用 Ctrl+C 正常停止；背景服務可在另一個 PowerShell 執行：

```powershell
.\scripts\stop.ps1
```

停止服務不等於平倉。需要全部退出時，先 `平倉`、確認 MT5 已無本系統持倉，再停服務。

第一次驗證正常後可背景啟動：

```powershell
.\scripts\start.ps1
```

日誌在 `runtime/service.out.log`、`runtime/service.err.log`。服務為本機背景程序，尚未安裝成 Windows Service；重開機後手動啟動，或由你自行配置工作排程器。不要同時啟動兩份；鎖會阻擋同一 bridge 的第二份。

## 10. 報告與常見問題

```powershell
.\.venv\Scripts\python.exe -m aitrader.cli --config .\config\local.json report
.\.venv\Scripts\python.exe -m aitrader.cli --config .\config\local.json replay
```

`report` 包含已平倉部位數、含佣金及 swap 的淨結果、抽樣權益回撤、API 次數與 token。API 帳單金額取不到時是 null，不會標成零。`replay` 只重播保存的介面資料，不是獲利回測。

| 症狀 | 檢查 |
| --- | --- |
| 找不到環境變數 | 在啟動服務的同一 PowerShell 執行 set-secrets，或重開 PowerShell |
| `Configure exact demo account...` | local.json 的 account/server 尚未填入 |
| `FileNotFoundError`／snapshot identity mismatch | EA 是否成功掛載、Common Files 路徑、帳號、server、magic 是否一致 |
| 商品不 ready | 休市／報價過期、K 棒未載入、佣金仍為 -1、商品名稱有歧義 |
| Telegram 不回覆 | token、user_id/chat_id、私人聊天、是否先 Start、有無另一程式或 webhook 使用同 bot |
| API 429 | 免費層／專案限流，程式退避 15 分鐘；核對 AI Studio 實際配額 |
| `API local quota/cooldown reached` | 本機每日或最小間隔限制；策略對話與分析共用額度 |
| `minimum lot exceeds budget` | 最小手數大於風險預算，正常跳過；不會自動加大風險 |
| `UNCERTAIN` | 成交狀態不明，停止新單；先在 MT5 核對實際持倉，再決定是否重新啟動 |
| `EA state=false` | 風控紀錄缺失、損毀或寫入失敗；保留檔案供排查，勿刪除紀錄強行繞過 |

至少觀察四週且 100 筆已平倉交易；停機期間不算有效觀察證據，需另外核對連續性。達到數量不是實盤批准，也不能證明策略未來獲利。

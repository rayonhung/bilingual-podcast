# 雙語 Podcast 小幫手

聽外語 podcast，邊聽邊讀雙語字幕。貼一個 RSS 連結或上傳 MP3，程式會自動轉錄（語音轉文字）並翻譯成你選的語言，再做成可逐句點擊跳轉的播放器。

轉錄與翻譯都用 [Groq](https://console.groq.com/keys) 的免費 API（金鑰只存在你自己的瀏覽器，不會上傳到任何地方）。

---

## 怎麼跑（本機）

只需要 **Python 3**，不必安裝任何套件。

```bash
cd ~/Desktop/podcast
python3 serve.py
```

它會自動開瀏覽器；若沒有，手動開終端機印出的網址（預設 `http://localhost:8000`）。
要關掉就回終端機按 `Ctrl+C`。

> **改完 code 直接重跑就好。** 啟動時會自動關掉還開著的舊 `serve.py`、自己接手 8000 埠，
> 所以瀏覽器重新整理一定是最新版，不會連到舊的那個。

## 怎麼用

1. 貼上 Groq API 金鑰（免費申請：<https://console.groq.com/keys>）。
2. 選翻譯語言與轉錄品質。
3. **貼 RSS 連結**：貼上 podcast 的 feed 網址 → 讀取 → 點任一集。
   （不知道 RSS？到 <https://podcastindex.org> 搜節目名稱、複製 feed 連結。）
   - 標「內含字幕」的單集會直接抓現成字幕、跳過轉錄，超快。
   - 標「需轉錄」的會用 AI 轉錄。
4. **上傳 MP3**：適合 Spotify 獨家或已下載的檔案。

---

## 主要功能與設計

- **長音檔自動切段**：超過 Groq 25MB 上限的 MP3，會依 MP3 frame 邊界自動切成多段
  （每段 < 20MB），逐段轉錄後把字幕時間軸準確接回去。純 Python、不需 ffmpeg。
  （目前自動切段僅支援 MP3；其他格式的超大檔請先剪短。）
- **暫時性錯誤自動重試**：Groq 偶發的 429 / 5xx（過載、`service_unavailable`）會以
  指數退避（1、2、4、8 秒）重試最多 5 次；像 401（金鑰錯）這種非暫時性錯誤則直接回報。
- **被擋時用 curl 後援**：來源網站或 API 回 403／406／429 時，改用 curl 重抓。
- **結果快取**：同一集 + 同一翻譯語言的結果會存在瀏覽器 `localStorage`，再開秒載。

## 架構

兩個檔案：

| 檔案 | 角色 |
|------|------|
| `index.html` | 前端：單一檔案的 HTML/CSS/JS，播放器與介面 |
| `serve.py` | 後端：本機迷你伺服器，代前端做瀏覽器被擋的事（讀 RSS、抓字幕、下載音檔、呼叫 Groq） |

前端用相對路徑呼叫 `/api/*`，所以同一個伺服器一起提供網頁與 API。

主要 API 端點（都在 `serve.py` 的 `do_POST`）：

- `POST /api/feed` — 解析 RSS，回傳單集清單（標題、日期、音檔、字幕連結）
- `POST /api/transcript` — 抓並解析現成字幕（SRT / VTT / podcast JSON）
- `POST /api/transcribe` — 用音檔網址下載後，呼叫 Groq 轉錄（必要時切段）
- `POST /api/transcribe_upload` — 上傳的 MP3（base64）轉錄
- `POST /api/translate` — 呼叫 Groq 把字幕分批翻譯

---

## 部署到網路上（非本機）

這個 app 需要常駐伺服器（會下載大檔、跑數分鐘的 AI），**不能只丟靜態空間**，
也**不適合 Vercel**（serverless 單次執行 60 秒就超時）。請用能跑常駐 Python 服務的平台：

**推薦：[Render](https://render.com) 或 [Railway](https://railway.app)（最省事）**

以 Render 為例：

1. 把 `serve.py` + `index.html` 放進一個 GitHub repo。
2. Render → New → **Web Service** → 連這個 repo。
3. 設定：
   - Runtime：**Python 3**
   - Build Command：留空（沒有套件要裝）
   - Start Command：`python3 serve.py`
4. 部署完會給你一個 `https://你的名字.onrender.com` 公開網址。

`serve.py` 已經做好部署準備：偵測到平台的 `PORT` 環境變數時，會自動綁 `0.0.0.0`、
不開瀏覽器、也不會去砍其他程序。

> 安全性：金鑰是每個使用者自己在瀏覽器輸入、隨請求帶上的，伺服器本身沒有任何密鑰，
> 所以公開部署不會外洩你的 Groq 額度。但它等於開了一個「能幫人下載 URL + 呼叫 Groq」
> 的公開代理；若要擋外人，可再自行加一層密碼。

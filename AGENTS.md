# 新高値デイリーボード：マルチエージェント版の手順（司令塔用）

毎営業日の大引け後に、司令塔（定期タスクで起動する Claude）が専門エージェントに仕事を振り分け、`<日付>/index.html` を公開する。
データの取り方と公開の細かい手順は [ROUTINE.md](ROUTINE.md) を正とし、このファイルは「誰が・どの順で・何を受け渡すか」を決める。

```
            ┌─────────── 司令塔（あなた） ───────────┐
            │ 休場判定 → 前日素材 → データ取得（ブラウザ）│
            │ → 指標計算（tools/indicators.py）        │
            └──────┬───────────┬──────────┬──────┘
                   ▼ 並行        ▼ 並行       ▼ 並行
            テクニカル担当   ニュース・地合い担当   リスク担当
                   └───────────┬──────────┘
                               ▼
                           編集担当（mid.html・meta.json）
                               ▼
                           チェック担当（数値の照合・ルール違反の指摘）
                               ▼
            司令塔：直す（最大2回）→ GitHub に公開（ブラウザ）→ 表示確認 → 報告
```

## 守ること
- ブラウザ（株探・X・GitHub）を触るのは司令塔だけ。ブラウザは1つなので、サブエージェントには使わせない。
- サブエージェントは作業フォルダのファイルだけを読み書きする。Web 検索・WebFetch もしない（数値の出所を取得データに限るため）。
- 受け渡しは決めたファイル名と形式で行う。サブエージェントの返事（チャットの文章）は使わず、ファイルを正とする。
- 計算はスクリプト、文章と判断の言語化はエージェント。移動平均・高値・安値・損切距離をエージェントに計算させない。
- 売買を推奨しない。推測は「〜とみられる」。

## 作業フォルダ
作業日を `D`、前営業日を `P` とする。クラウドの作業ディレクトリに `work/D/` を作る。

```
work/D/
  prev/mid.html, prev/meta.json        … 前営業日の公開物（raw.githubusercontent.com から）
  raw/data.txt                         … 株探 SEC/UP/DOWN/HIGH（ROUTINE.md 1章の JS の結果そのまま）
  raw/indices.md                       … 指数・売買代金・騰落数・先物夜間・ドル円（取得時刻つき）
  raw/8306.md, raw/7685.md             … 持ち株の株価・PTS・PER/PBR・利回り・信用残・個別ニュース
  raw/8306_daily.csv, raw/7685_daily.csv … 日足 date,open,high,low,close,volume（9ページ分）
  raw/news.md                          … 市況記事の要点（記事名・時刻・要点。本文の丸写しはしない）
  raw/x.md                             … X の注目投稿の要点（投稿者・時刻・要点）。取れなければ「ログイン切れのため未取得」
  calc/8306.json, calc/7685.json       … tools/indicators.py の出力
  out/technical.json, out/market.json, out/risk.json … 分析担当の成果
  out/mid.html, out/meta.json          … 編集担当の成果
  out/review.json                      … チェック担当の成果
```

## 手順

### 1. 準備（司令塔）
- 東証の休場日なら「休場日のためスキップ」と報告して終了。
- このリポジトリを `git clone --depth 1 https://github.com/uofu0114/stock` で取得する（読み取りのみ。役割カードとスクリプトを使うため）。
- `P` の `mid.html` と `meta.json` を `work/D/prev/` に保存し、持ち株の損切ラインを読み取る（meta.json の `lines` の「損切」）。

### 2. データ取得（司令塔・ブラウザ）
ROUTINE.md の 1章・2章のとおりに取得し、上の `raw/` に保存する。日足は ROUTINE.md の JS と同じ方法で `/stock/kabuka?code=XXXX&ashi=day&page=1..9` の表を読み、CSV にする。ページの日付表示が作業日 `D` と一致することを確認する。

### 3. 指標計算（司令塔・スクリプト）
```
python3 tools/indicators.py work/D/raw/8306_daily.csv --stop <8306の損切ライン> > work/D/calc/8306.json
python3 tools/indicators.py work/D/raw/7685_daily.csv --stop <7685の損切ライン> > work/D/calc/7685.json
```

### 4. 分析（サブエージェント3人を同時に起動）
Agent ツールで、次の3人を **1回の返答の中で同時に** 起動する（subagent_type は general-purpose）。
プロンプトには、役割カード（`agents/` のファイル）の全文、作業フォルダの絶対パス、日付 `D` と `P` を書く。
- テクニカル担当：`agents/technical.md` → `out/technical.json`
- ニュース・地合い担当：`agents/market.md` → `out/market.json`
- リスク担当：`agents/risk.md` → `out/risk.json`

3人とも終わったら、3つのファイルがあり JSON として読めることを確かめる。欠けていたらその担当だけもう一度起動する。

### 5. 編集（サブエージェント）
編集担当 `agents/editor.md` を起動し、`out/mid.html` と `out/meta.json` を作らせる。

### 6. チェック（サブエージェント・独立）
チェック担当 `agents/reviewer.md` を起動する。プロンプトには役割カードと作業フォルダのパスだけを渡し、分析担当や編集担当の返事は渡さない（作った側の説明に引っ張られないため）。
`out/review.json` の `issues` に `severity: "error"` があれば、編集担当にその一覧を渡して直させ、もう一度チェックする。2回直しても残る error は、公開ページの結論の末尾に「確認できなかった点」として正直に書く。

### 7. 公開（司令塔・ブラウザ）
ROUTINE.md の 4章のとおり、4ファイルをコミットする。
- `daily/D/mid.html` ← `out/mid.html`
- `daily/D/meta.json` ← `out/meta.json`
- `daily/D/data.txt` ← `raw/data.txt`
- `D/index.html` ← ROUTINE.md の固定の中身
各ファイルを raw.githubusercontent.com から読み戻して一致を確認し、数分後に `https://uofu0114.github.io/stock/D/` の表示を確認する。

### 8. 報告（3〜6行）
公開URL、日経平均とTOPIXの終値、持ち株2銘柄の終値と損切ラインまでの距離（%）、撤退ライン到達の有無、チェック担当が見つけて直した点の件数、うまくいかなかった手順。

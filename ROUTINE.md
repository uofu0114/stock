# 毎日のルーティン（Claude 用の作業手順）

新高値投資塾（DUKE。さん）の塾生、たかたかさんの大引け後ルーティン。平日 19 時ごろに実行し、`<日付>/index.html` を公開する。東証の休場日（土日祝・年末年始）は何もしない。

## 0. 準備
- 作業日の日付を `D`（例 2026-10-08）とする。
- 前営業日の素材を `https://raw.githubusercontent.com/uofu0114/stock/main/daily/<前営業日>/mid.html`（と meta.json）から読み、持ち株の損切ラインや前日の見立てを引き継ぐ（WebFetch か、ブラウザペインの fetch で）。

## 1. データ取得（株探はユーザーPCのブラウザペインで開き、ページ内 JavaScript の fetch で取る）
株探は WebFetch では 403 になる。Claude のブラウザペインで `https://kabutan.jp/warning/?mode=9_1` を開き、javascript_tool で次を実行する。

```js
async function doc(u){const h=await (await fetch(u)).text();return new DOMParser().parseFromString(h,'text/html');}
async function grab(url){const d=await doc(url);const t=d.querySelector('table.stock_table');if(!t)return[];return [...t.querySelectorAll('tr')].slice(1).map(r=>[...r.children].map(x=>x.innerText.trim())).filter(c=>c.length>5);}
async function all(m,max){let rows=[];for(let p=1;p<=max;p++){const g=await grab(`/warning/?mode=${m}&market=0&capitalization=-1&dispmode=normal&stc=&stm=0&page=${p}`);rows.push(...g);if(g.length<15)break;}return rows;}
const sec=await all('9_1',3), up=await all('2_1',7), dn=await all('2_2',7), hi=await all('3_3',20);
const f=r=>[r[0],r[1],r[2],r[5],r[7],r[8],r[9],r[10],r[11],r[12]].join('|');
'SEC\n'+sec.map(r=>r.join('|')).join('\n')+'\nUP\n'+up.slice(0,100).map(f).join('\n')+'\nDOWN\n'+dn.slice(0,100).map(f).join('\n')+'\nHIGH\n'+hi.filter(r=>r[2]!='東Ｅ').map(f).join('\n')
```
結果をそのまま `daily/D/data.txt` に保存する。ページの日付表示（「2026年10月07日」）が作業日と一致することを確認する。

指数・持ち株・ニュースも同じ方法で取る。
- 指数：`/stock/?code=0000`（日経平均）、`0010`（TOPIX）、`0012`（グロース250）、`0950`（ドル円）の `#stockinfo_i1`
- 持ち株：三菱UFJ `8306`、バイセル `7685`。`/stock/?code=` で株価・PTS・PER/PBR・信用残、`/stock/kabuka?code=XXXX&ashi=day&page=1..9` の日足で 5/25/75/200 日移動平均・52 週高値・20 日安値を算出。直近 120 営業日の終値を「MM/DD:終値」を新しい順にスペース区切りで並べ、`meta.json` の `closes` にする。
- ニュース：`/news/marketnews/?category=1`（2〜3 ページ）から当日の「日経平均 大引け」「明日の株式相場に向けて」、夜間の「日経225先物」「ダウ」を読む。持ち株の個別ニュース `/stock/news?code=XXXX`。

## 2. X（フォロー先の注目情報）
ブラウザペインで `https://x.com/home` を開き「株式投資」リストのタブを読む。DUKE。さん `https://x.com/investorduke` の当日の投稿も確認する。ログインが切れていたら X の欄に「ログイン切れのため未取得」と書き、ほかは続ける（パスワードは入力しない）。

## 3. ページの中身（daily/D/mid.html と meta.json）
`daily/<前営業日>/mid.html` と同じ構造で書き直す。セクションは次の順。
1. 今日の結論（3 段落：結論／何が起きたか／持ち株）
2. 指数 6 枠（日経平均、TOPIX、グロース250、プライム売買代金と騰落数、日経225先物の夜間、ドル円）
3. 本日の地合い（テーマ 4 枠＋年初来高値の件数）
4. 持ち株 2 枠（状態ピル、株価、チャート、指標、状態・気になる点・どうするか、損切ライン）
5. 明日の注目（カレンダーと見立て）
6. X フォロー先の注目情報

判断のルール
- 新高値ブレイク投資の考え方に沿う：上昇トレンド（株価＞25日＞75日＞200日）は保有、52 週高値の終値更新は買い増し候補、下降トレンドは買い増ししない。
- 損切ラインは「直近 20 日安値の終値割れ」を基本に、毎日見直す。ラインを終値で割ったら、mid.html の結論の先頭で「撤退ラインに到達」と明記する。
- 数値はすべて取得したデータから。推測は「〜とみられる」と書く。売買を推奨しない。

`meta.json` の例：
```json
{"headline":"2026年10月7日（水）大引け ／ 作成 10月7日 19時台","summary":"一覧ページ用の 1 行要約",
 "charts":{"8306":{"closes":"10/07:3622 10/06:3671 ...","lines":[[3813,"52週高値","var(--up)"],[3541,"損切 3,541","var(--warn)"]]},
           "7685":{"closes":"10/07:2765 ...","lines":[[2681,"損切 2,681","var(--warn)"],[2923,"25日線 2,923","var(--muted)"]]}}}
```

## 4. 公開（GitHub へのコミットはブラウザペインで行う）
クラウド側からは GitHub に書き込めないため、ブラウザペイン（uofu0114 でログイン済み）の GitHub 画面からファイルを作成する。1 ファイルずつ次を繰り返す。
1. `https://github.com/uofu0114/stock/new/main?filename=<パス>` を開く（例 `daily/2026-10-08/mid.html`）。
2. javascript_tool で本文を貼り付ける：
```js
const txt = <ファイル内容の文字列>;  // 長い本文は関数のブロックコメントに埋め込んで toString() し、前後を切り取ると、引用符やバッククォートをエスケープせずに渡せる（本文にコメント終端の記号が無いこと）
const el=document.querySelector('.cm-content');el.focus();const dt=new DataTransfer();dt.setData('text/plain',txt);
el.dispatchEvent(new ClipboardEvent('paste',{clipboardData:dt,bubbles:true,cancelable:true}));
await new Promise(r=>setTimeout(r,500));
[...document.querySelectorAll('button')].find(b=>b.innerText.trim()==='Commit changes...').click();
await new Promise(r=>setTimeout(r,1500));
[...document.querySelector('[role="dialog"]').querySelectorAll('button')].find(b=>b.innerText.trim()==='Commit changes').click();
```
3. `https://raw.githubusercontent.com/uofu0114/stock/main/<パス>` を fetch して、中身が意図どおりか確認する。

毎日作るファイルは 4 つ：`daily/D/mid.html`、`daily/D/data.txt`、`daily/D/meta.json`（summary 付き）、`D/index.html`（下の固定の中身）。
```html
<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex"><title>新高値デイリーボード</title></head><body><p>読み込み中…</p><script src="../tools/render.js"></script></body></html>
```
`D/index.html` は `tools/render.js` がテンプレート（`tools/template.html`）と `daily/D/` の素材を読み込んで表示する。トップの `index.html` は GitHub API で `daily/` の日付フォルダを一覧にするので、更新不要。数分後に `https://uofu0114.github.io/stock/D/` で見られる。

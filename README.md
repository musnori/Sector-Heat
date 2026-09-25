# 資金の温度計

どのチェーン・セクターにお金が流れているかを1時間ごとに集計して、Webのダッシュボードで表示するツールです。GitHub Actions（データ集め）と GitHub Pages（表示）だけで動くので、サーバー代はかかりません。

## 見ているもの

| 指標 | 取得元 | 意味 |
|---|---|---|
| ステーブルコイン残高の7日変化 | DefiLlama | 実需のお金がそのチェーンに入ってきているか |
| DEX出来高の7日変化 | DefiLlama | 実際に取引が活発になっているか |
| TVLの7日変化 | DefiLlama | DeFiに預けられているお金の増減 |
| 建玉(OI)の24時間変化 | Binance（ダメならOKX） | レバレッジのお金がどれだけ入ったか |
| 資金調達率(FR) | Binance（ダメならOKX） | ロングが混みすぎていないか |
| セクター時価総額の対BTC | CoinGecko | ソラナ系・AI系・ミーム系などの勢い |
| BTCドミナンス | CoinGecko | 資金がBTCに集まっているか、アルトに回っているか |
| 価格トレンド（7日線・30日線） | Binance（ダメならOKX）の4時間足 | 上昇の流れに乗っているか、落ちている最中か |
| OIと価格のズレ | 上の2つから計算 | 新しい買いで上がっているのか、OIだけ膨らんで危ないのか |
| 恐怖・強欲指数 | alternative.me | 相場全体の雰囲気（恐怖＝買い場になりやすい、強欲＝天井に注意） |
| 注目トークン | DefiLlama + CoinGecko | 各チェーンのDeFiトークンのうち、資金（TVL）が入っていて価格がBTCより強い／まだ出遅れているもの |

温度60以上で「静かに流入中」＋「上昇トレンド」＋「OIに危ないズレなし」＋「恐怖・強欲指数が75未満」がそろったチェーンのうち、温度の高い上位3つに **◎ 条件そろい** が付きます。エントリーを検討する候補の目安です。

## セットアップ（15分くらい）

1. **CoinGeckoのDemo APIキーを取る**
   coingecko.com でアカウントを作り、Developer Dashboard から Demo キーを発行します（無料）。

2. **GitHubにリポジトリを作ってファイルを置く**
   このフォルダの中身をそのまま push します。

3. **キーをSecretsに登録する**
   リポジトリの Settings → Secrets and variables → Actions → New repository secret
   名前: `COINGECKO_API_KEY` / 値: 手順1のキー
   ※キーはコードやチャットに直接貼らないでください。

4. **GitHub Pagesを有効にする**
   Settings → Pages → Source を「Deploy from a branch」、Branch を `main` と `/docs` にして保存します。

5. **初回のデータ取得を手動で動かす**
   Actions タブ → `update-data` → Run workflow。
   成功すると `docs/data.json` がコミットされ、Pagesの URL（`https://<ユーザー名>.github.io/<リポジトリ名>/`）で表示されます。以降は毎時自動で更新されます。

## 手元で見た目を確認する

```bash
pip install -r requirements.txt
python scripts/fetch_data.py --mock      # サンプルデータを作る
python -m http.server -d docs 8000
# ブラウザで http://localhost:8000/?mock を開く
```

本番データで試す場合は `COINGECKO_API_KEY=xxxx python scripts/fetch_data.py` を実行します。

## 知っておいてほしいこと

- **先物データの地域ブロック**: GitHub Actions はアメリカのサーバーで動くため、Binance は高確率でブロックされます。その場合は自動で OKX を試します。両方ダメなときは FR と OI なしで計算し、画面に警告を出します。確実に取りたい場合は、自分のPCを self-hosted runner にするか、手元で cron を回して push する方法があります。
- **履歴は溜まるほど賢くなる**: セクターの7日変化とBTCドミナンスの7日の流れは、このツール自身が貯めた履歴から計算します。動かし始めて7日後から表示されます。
- **Actionsの遅延と停止**: スケジュール実行は混雑時に数分〜十数分遅れることがあります。また、リポジトリに60日間動きがないと GitHub がスケジュールを止めることがあるので、止まっていたら Actions タブから再開してください。
- **スコアは目安**: 順位の合成なので、全体が下げ相場でも「相対的にマシな所」が上に来ます。

## カスタマイズ

`scripts/fetch_data.py` の上の方で変えられます。

- `CHAINS`: 監視するチェーンと先物銘柄の組み合わせ
- `WEIGHTS`: 各指標の重み
- `FR_HOT` / `FR_CALM` / `OI_HOT`: 「過熱気味」「静かに流入中」の判定ライン
- `MIN_CAT_MCAP` / `MAX_CATS`: 表示するセクターの最低時価総額と件数
- `PX_MOVE` / `OI_MOVE` / `OI_SURGE`: OIと価格のズレを判定するライン
- `FNG_GREED`: これ以上の強欲では「条件そろい」を出さない
- `TOKENS_PER_CHAIN` / `TOKEN_MIN_TVL` / `TOKEN_MIN_MCAP`: 注目トークンの件数と足切りライン

チェーン名は DefiLlama の表記に合わせる必要があります（見つからないものはログに `skip` と出ます）。

## 次にやれそうなこと

- 「静かに流入中」に切り替わったチェーンを Discord に通知する
- 清算データ（Binance の WebSocket `forceOrder`）を足す
- チェーンをクリックすると、そのエコシステムの上位トークンが見える

投資判断はご自身の責任でお願いします。このツールは判断材料を集めるためのものです。

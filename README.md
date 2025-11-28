# EdgeX Grid Bot 修正版 (Koyeb)

## 1. 設置方法

## 1.1 [Koyeb](https://app.koyeb.com/services/new)を開く

[Koyeb - service new](https://app.koyeb.com/services/new)を開く

必ず`Worker`かつ`Github`を選択すること

<img src="image-1.png" width="75%" height=75%>

Public Github Repositoryに以下のURLを入れる

`https://github.com/zaramcei/edgex-grid`

<img src="image-2.png" width="75%" height=75%>

必ず以前稼働していたリージョンと同じ場所、同じCPUを選択する

<img src="image-4.png" width="75%" height=75%>

Dockerfileを選択

<img src="image-3.png" width="75%" height=75%>

## 1.2 環境変数を以下の解説に従って設定する

<img src="image-6.png" width="75%" height=75%>

`Environment variable and files`を開く

以下の画像はあくまで設定例なのでこの画像の通りに設定せず、指示されたパラメータで稼働させてください。

<img src="image.png" width="75%" height=75%>

設定したら`Save and deploy`を押して起動する

# 2. 設定値の解説

## 2.1 基本設定（必須）

- `EDGEX_ACCOUNT_ID`: あなたのEdgeXアカウントID（数値）
- `EDGEX_STARK_PRIVATE_KEY`: あなたの秘密鍵
- `EDGEX_BASE_URL`: EdgeX APIのベースURL（デフォルト: `https://pro.edgex.exchange`）
- `EDGEX_CONTRACT_ID`: 取引する銘柄のID（例: BTC-PERPは `10000001`）
- `EDGEX_LEVERAGE`: 取引レバレッジ倍率（例: `100`）- 損益率の計算に使用


## 2.2 グリッド設定

- `EDGEX_GRID_STEP_USD`: グリッド幅（USD）- 価格レベル間の間隔（例: `10`, `50`, `100`）
- `EDGEX_GRID_FIRST_OFFSET_USD`: 中央価格からの初回オフセット（USD）（例: `10`, `50`）
- `EDGEX_GRID_LEVELS_PER_SIDE`: 片側（買い/売り）のグリッド本数（例: `5`, `10`）
- `EDGEX_GRID_SIZE`: 1本あたりの注文数量（BTC）（例: `0.001`, `0.002`）
- `EDGEX_GRID_OP_SPACING_SEC`: 注文間の待機時間（秒）- レート制限回避用（デフォルト: `0.1`）

## 2.3 ポジションベースのロスカット/利確設定（設定が難しくロスカットが頻発するので非推奨）

基本的には2.4のアセットベースのロスカットの方を設定してください。

保有中のポジションの未実現損益（レバレッジを考慮）が指定の閾値に達したら、そのポジションを成行注文で自動的にクローズする機能です。

**挙動:**
- ポジション単位で個別に損益を監視
- 損益率 = (未実現損益 / ポジション価値) × レバレッジ倍率
- 閾値に達した瞬間に成行でポジションクローズ
- クローズ後、すぐに通常のグリッド取引を再開

**設定:**
- `EDGEX_POSITION_LOSSCUT_PERCENTAGE`: ポジションの未実現損益が指定%以下になったら自動ロスカット（例: `10` = -10%でロスカット）
  - `null`または未設定の場合はロスカットを実行しない
- `EDGEX_POSITION_TAKE_PROFIT_PERCENTAGE`: ポジションの未実現損益が指定%以上になったら自動利確（例: `10` = +10%で利確）
  - `null`または未設定の場合は利確を実行しない

## 2.4 資産ベースのロスカット/利確設定

口座全体の総資産（残高+未実現損益）を監視し、ポジション開始時の資産を基準にして指定%の変動があった場合に全ポジションをクローズする機能です。ポジション単位ではなく口座全体の資産を保護します。

**挙動:**
- 総資産 = 現在の残高 + 全ポジションの未実現損益
- 基準資産（initial_asset）はポジションを持っていない時の残高
- ポジション保有中に総資産が基準資産から指定%変動したら発動
- 発動時の処理:
  1. 全ポジションを成行で即座にクローズ
  2. 全ての未約定注文をキャンセル
  3. クローズ完了を確認後、基準資産を現在の残高に更新
  4. 30秒のクールダウン後、通常のグリッド取引を再開
- 次回ポジションは新しい基準資産から損益を計算

**設定:**
- `EDGEX_ASSET_LOSSCUT_PERCENTAGE`: 総資産（残高+未実現損益）がポジション開始時の資産から指定の%減少したら全ポジションクローズ（デフォルト: `3.0` = -3%）
  - 例: `0.05`に設定すると、総資産が-0.05%（約-0.2 USD at 400 USD）減少時にロスカット
  - ポジション単位ではなく、口座全体の資産を保護する機能
- `EDGEX_ASSET_TAKE_PROFIT_PERCENTAGE`: 総資産が初期資産から指定%増加したら全ポジションクローズ（デフォルト: `5.0` = +5%）
  - `0`または未設定の場合は利確を実行しない

## 2.5 全戻し全決済による資産リカバリー設定

資産が減少した後のポジションで含み益が出ているケースのうち、未実現損益と現在の（減少した）資産価格の合計値が初期資産額に到達したらポジションを全部クローズする機能に関する設定

- `EDGEX_BALANCE_RECOVERY_ENABLED`: バランスリカバリー機能の有効/無効（`true` or `false`）
  - 残高が初期残高まで回復したら全ポジションをクローズする機能
- `EDGEX_INITIAL_BALANCE_USD`: 初期残高（USD）- リカバリーの基準値（例: `400.0`）
- `EDGEX_RECOVERY_ENFORCE_LEVEL_USD`: リカバリーモードを有効にする最小損失額（USD）（デフォルト: `3.0`）
  - 初期残高から指定額以上の損失が出た場合のみリカバリーモードが発動

## 2.6 稼働スケジュール設定

リモートのJSONファイルから稼働スケジュールを取得し、指定された時間帯のみBotを稼働させる機能です。

- スケジュールURL: `https://zaramcei.github.io/edgex-grid/schedule/schedule.json`
- スケジュールが取得できない場合でも、既存のスケジュール情報で動作を継続

**スケジュール機能の有効/無効:**
- `EDGEX_USE_SCHEDULE`: スケジュール機能の有効/無効（デフォルト: `true`）
  - 未設定または空欄: スケジュール機能が有効
  - `false`, `0`, `no`: スケジュール機能が無効（常時稼働）

**スケジュールタイプの選択:**
- `EDGEX_USE_SCHEDULE_TYPE`: 使用するスケジュールタイプ（デフォルト: `normal`）
  - `normal`: 通常稼働（短めの稼働時間）
  - `aggressive`: 攻め稼働（長めの稼働時間）
  - スケジュールJSONに定義された任意のタイプ名を指定可能

**設定例:**
```
EDGEX_USE_SCHEDULE_TYPE=normal     # 通常稼働スケジュールを使用
EDGEX_USE_SCHEDULE_TYPE=aggressive # 攻め稼働スケジュールを使用
```

**挙動:**
- 5分ごとにリモートからスケジュールJSONを取得・更新
- スケジュール時間外の場合、グリッド注文は発注されず待機状態になる
- `lot_coefficient`: グリッド注文のサイズに掛ける係数（例: `0.5`なら通常の0.5倍のサイズで注文）

**スケジュール外への移行時の動作:**

稼働時間内から時間外に移行した際の動作を環境変数で制御できます。

- `EDGEX_OUT_OF_SCHEDULE_ACTION`: スケジュール外に出た時の動作（デフォルト: `auto`）
  - `nothing`: 全指値注文をキャンセルし、ポジションはそのまま維持して終了。裁量でクローズしたい場合に使用
  - `auto`: 現在価格から5ドル有利な価格で指値クローズ注文を出し、1分以内に約定しなければ成行でクローズすることで注文手数料の削減とリスク管理を両立
  - `immediately`: 即座に成行でポジションをクローズ、注文手数料はTAKERだが損失拡大リスクは即座に無くなる


## 2.7 ポジションサイズ制限（REDUCE_MODE）

ポジションサイズが一定の閾値を超えた場合、ポジションを積み増す方向の注文をスキップし、ポジションを減らす方向の注文のみを許可する機能です。

BTCとRATIOの2種類から**どちらか一方のみ**を設定する必要があります。両方設定した場合や両方未設定の場合、`LIMIT` より `REDUCE_ONLY`の方が大きい（矛盾していて意味のない設定）場合は起動時にエラーで終了します。

### BTC絶対値による制限

ポジションサイズ（BTC）の絶対値で制限します。

- `EDGEX_POSITION_SIZE_LIMIT_BTC`: この値以上で`REDUCE_MODE`に突入（例: `0.1`）
- `EDGEX_POSITION_SIZE_REDUCE_ONLY_BTC`: この値を下回るまで積み増し禁止（例: `0.05`）

**例:**
```
EDGEX_POSITION_SIZE_LIMIT_BTC=0.1
EDGEX_POSITION_SIZE_REDUCE_ONLY_BTC=0.05

ポジション: 0.08 BTC → 通常モード（BUY/SELL両方OK）
ポジション: 0.10 BTC → REDUCE_MODE突入（積み増し禁止）
ポジション: 0.06 BTC → REDUCE_MODE継続
ポジション: 0.04 BTC → REDUCE_MODE解除（BUY/SELL両方OK）
```

### RATIO（総資産比率）による制限

ポジション価値が総資産に対して何%かで制限します。
動作確認時は

- `EDGEX_POSITION_SIZE_LIMIT_RATIO`: `0.5`
- `EDGEX_POSITION_SIZE_REDUCE_ONLY_RATIO`: `0.25`

の設定がおすすめです。

**計算式:**
```
current_ratio = (現在BTC価格 × ポジションサイズ) / 総資産(initial_asset)
```

- `EDGEX_POSITION_SIZE_LIMIT_RATIO`: この割合(%)以上で`REDUCE_MODE`に突入（例: `50.0`）
- `EDGEX_POSITION_SIZE_REDUCE_ONLY_RATIO`: この割合(%)を下回るまで積み増し禁止（例: `30.0`）

**例:**
```
EDGEX_POSITION_SIZE_LIMIT_RATIO=50.0
EDGEX_POSITION_SIZE_REDUCE_ONLY_RATIO=30.0

総資産: 10,000 USD / BTC価格: 100,000 USD

ポジション: 0.04 BTC → ratio=40% → 通常モード
ポジション: 0.05 BTC → ratio=50% → REDUCE_MODE突入
ポジション: 0.035 BTC → ratio=35% → REDUCE_MODE継続
ポジション: 0.025 BTC → ratio=25% → REDUCE_MODE解除
```

**挙動:**
1. ポジションサイズが `LIMIT` 値以上になると `REDUCE_MODE` に突入
2. `REDUCE_MODE` 中はポジションを積み増す方向の注文をスキップ
   - LONG保持中: BUY注文をスキップ、SELL注文のみ許可
   - SHORT保持中: SELL注文をスキップ、BUY注文のみ許可
3. ポジションサイズが `REDUCE_ONLY` 値を下回ると `REDUCE_MODE` を解除
4. 通常モードに戻り、両方向の注文を再開

**設定方法:**


**バリデーション:**
- BTCとRATIOの両方を設定 → エラー終了
- BTCもRATIOも未設定 → エラー終了
- LIMITのみ設定してREDUCE_ONLY未設定 → エラー終了
- REDUCE_ONLY >= LIMIT → エラー終了（REDUCE_ONLYはLIMITより小さい必要あり）
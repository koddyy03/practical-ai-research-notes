# セットアップ — Kodai が手を動かすのはここだけ

自動化できないのは「本人確認が絡む3箇所」だけ。それ以外は全部コード側で持つ。

| # | やること | 所要 | 頻度 |
|---|---------|------|------|
| 1 | TikTok 開発者アプリを作る | 約10分 | 1回 |
| 2 | 審査を申請する（文面は用意済み・貼るだけ） | 約5分 | 1回 |
| 3 | 認可リンクを開いて「許可」を押す | 約30秒 | 年1回 |

この3つ以外（トークン更新・動画判定・キャプション生成・投稿・完了確認）は全自動。

---

## 1. 開発者アプリを作る

https://developers.tiktok.com/ → 投稿に使う TikTok アカウントでログイン → Manage apps → Connect an app

入力する内容:

- **App name**: `Silk The Rich Studio Poster`
- **Category**: Content publishing / Social media management
- **Products を追加**: `Content Posting API` を選び、**Direct Post** を有効にする
- **Scopes**: `user.info.basic` と `video.publish` の2つだけ
  - 余計なスコープを足すと審査でその分も説明を要求されるので増やさない
- **Redirect URI**: 下の「2-B」を参照

作ったら **Client key** と **Client secret** が出る。この2つを控える。

## 2-A. 審査（audit）を申請する

`AUDIT_APPLICATION.md` に申請文面をそのまま貼れる形で用意してある。コピペして送信。

**なぜ必須か**: 審査を通していないアプリは、`PUBLIC_TO_EVERYONE` を指定しても
TikTok 側で強制的に非公開にされる。つまり審査前は「公開投稿」ができない。
審査には実際に動いているデモ動画の提出が求められ、スコープごとに数週間かかる。

審査が降りるまでの間の運用は `README.md` の「審査待ちの間」を参照。

## 2-B. Redirect URI

ブラウザが認可後に戻ってくる先。サーバーを立てる必要はなく、
`?code=...` が見えれば十分なので、確実に存在する静的ページを使う。

推奨: `https://silktherich.com/tiktok-callback`（実在するページにする。404 でも
`code` は URL バーに出るが、TikTok 側がドメイン所有確認を求める場合があるため
自社ドメイン配下にしておくと通しやすい）

開発者コンソールの Redirect URI と、環境変数 `TIKTOK_REDIRECT_URI` を
**完全一致**させること。1文字でも違うと認可が通らない。

## 3. 認可を1回だけ通す

```bash
# 認可URLを出す
python3 -m tiktok_autopost.auth_setup url
```

出た URL を開いて「許可」。戻り先 URL の `code=` の値をコピーして:

```bash
python3 -m tiktok_autopost.auth_setup exchange \
    --code "<貼る>" --token-state token_state.json
```

これで `token_state.json` ができる。リフレッシュトークンは365日有効で、
更新ごとに自動でローテーションするので、通常は年1回しか触らない。

---

## 認証情報の置き場所

`TIKTOK_CLIENT_KEY` / `TIKTOK_CLIENT_SECRET` / `TIKTOK_REDIRECT_URI` を
実行環境の secrets に入れる。

`token_state.json` は **Google ドライブのログフォルダに置く**。
この実行コンテナは毎回作り直されるため、ローカルに置くと次回の更新で死ぬ。
定期実行の流れ:

1. ドライブから `token_state.json` を取得
2. 投稿処理を実行（トークンが更新され、ファイルが書き換わる）
3. 書き換わったファイルをドライブに戻す

**ローテーションしたリフレッシュトークンを書き戻し忘れると、次回の投稿が失敗する。**
ここが唯一の壊れやすい箇所なので、定期実行のプロンプトに手順として明記してある。

---

## ネットワーク

`open.tiktokapis.com` はこの実行環境のネットワークポリシーでブロックされている
（`curl` が CONNECT 403 を返す）。現状は内部ネットワークが通っているサンドボックス
経由で実行している。

環境設定で `open.tiktokapis.com` を許可リストに入れれば、コンテナから直接叩けるので
経路が1つ減って安定する。設定は1回で済む。

## 動作確認（投稿せずに検証だけ）

```bash
python3 -m tiktok_autopost.post_video \
    --video /tmp/clip.mp4 \
    --caption "she said yes at the grove" \
    --first-comment "she almost said no" \
    --dry-run
```

`--dry-run` は API を一切叩かない。縦型判定とキャプション検証だけ走る。

## テスト

```bash
python3 -m tiktok_autopost.tests.test_caption      # 17件
python3 -m tiktok_autopost.tests.test_posting      # 20件
python3 -m tiktok_autopost.tests.test_video_probe  # 25件
```

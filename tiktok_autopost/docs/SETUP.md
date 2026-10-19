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
`?code=...` が URL バーに出れば十分。

**ドメインの実情 (2026-10-19 実測)**:

| ドメイン | 中身 |
|---|---|
| `silktherich.com` | 別サイト。`/terms` `/privacy` `/policies/*` すべて 404 |
| `us.silktherich.com` | Shopify ストア (Basic プラン)。ポリシーページはここにある |

したがって Redirect URI は **Shopify 側のドメイン配下**に置く:
`https://us.silktherich.com/pages/tiktok-callback`

このページは未作成。Shopify 管理画面で空のページを 1 枚作るか、
Shopify API (`pageCreate`) で作る。404 のままでも認可コード自体は URL バーに
出るが、TikTok がドメイン所有確認を求める場合に通しやすいので実在させておく。

開発者コンソールの Redirect URI と、環境変数 `TIKTOK_REDIRECT_URI` を
**完全一致**させること。末尾スラッシュの有無でも認可が通らない。

## 2-C. 開発者アカウントと投稿先アカウントは別でよい

アプリを所有する開発者アカウントと、実際に投稿されるアカウントは別で問題ない。
投稿先は **`video.publish` を認可したアカウント**で決まる。

ただし審査前は Sandbox 経由になるため、**アプリの Sandbox に投稿先アカウント
(航大さんの個人アカウント) を Target User として追加する**必要がある。
これを忘れると、個人アカウントで認可を踏んでも通らない。
Sandbox は 1 アプリあたり 5 個まで、1 Sandbox に 10 アカウントまで登録できる。

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

## 別の環境で初回認可した場合の受け渡し

初回認可 (OAuth) はブラウザとログインがある環境でやるのが早い。手元の
パソコンで認可を済ませた場合、得られた **リフレッシュトークンだけ**を
この実行環境の secrets に `TIKTOK_REFRESH_TOKEN` として入れれば、
こちら側はそれを種にして動き出す (トークンファイルが無いときだけ参照する)。

**重要: リフレッシュトークンを更新できるのは片方だけ。**
トークンは更新ごとにローテーションし、古い値は即座に無効になる。
手元のスクリプトとこちらの定期実行が両方 refresh を走らせると、
互いのトークンを壊し合って投稿が止まる。初回認可が済んだら、
**以後 refresh を走らせるのはこちら側だけにする**(手元のスクリプトで
投稿やトークン更新をしない)。手元で投稿したいときは、こちらの定期実行を
一時停止してから使う。

トークンJSONのキー名は `refresh_token` / `refreshToken` のどちらでも読む。
`expires_in` (相対秒) しか無い形式は、発行時刻が不明なので期限切れとして
扱い、必ず refresh を走らせる。

## 認証情報の置き場所

**実行環境の「環境変数」欄に secret を置かないこと。** あの欄は平文で、
その環境を使う全員に見える (画面にもその旨の警告が出る)。
「ネットワークシークレット」欄のほうは逆に、プロキシがリクエストへ
差し込む方式でコードからは読めないため、こちらの用途には使えない
(TikTok のトークン更新は client_secret を本文に入れて送るため)。

したがって **Google ドライブのログフォルダに private な JSON を 1 枚置く**。
リフレッシュトークンは更新のたびに値が変わるので、どのみち書き戻せる
場所が要る。環境変数では更新後の値を保存できない。

`tiktok_credentials.json` (ログフォルダ `16hwi3Lp41_HEKWFmSS9gECgFuIucs-cU` 内):

```json
{
  "client_key": "...",
  "client_secret": "...",
  "refresh_token": "..."
}
```

`access_token` / `access_expires_at` は初回実行後に自動で書き足される。
`client_key` と `client_secret` はトークン更新で消えない (save はファイル内の
他のキーを保持する)。

定期実行の流れ:

1. ドライブから `tiktok_credentials.json` を取得して一時ファイルに置く
2. `--token-state <そのパス>` を付けて投稿を実行する
3. 書き換わったファイルをドライブに戻す

**3 を忘れるとローテーション後のリフレッシュトークンが失われ、次回の投稿が
失敗する。** ここが唯一の壊れやすい箇所なので、定期実行のプロンプトに
手順として明記してある。

秘密でない値 (`TIKTOK_CLIENT_KEY` など) だけなら環境変数でもよく、
ファイルに無ければ環境変数を見にいく。ファイルの値が優先される。

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

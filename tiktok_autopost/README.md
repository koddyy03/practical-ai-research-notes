# tiktok_autopost — "Can I Silk Your Hair?" 自動投稿

街頭シリーズのTikTok自動投稿。広告に見えた時点で数字が死ぬので、
**商品を売る言葉を1つでも出さない**ことをコードで強制する。
横動画は1本も投稿しない。

セットアップ手順は [`docs/SETUP.md`](docs/SETUP.md)、
審査申請の文面は [`docs/AUDIT_APPLICATION.md`](docs/AUDIT_APPLICATION.md)。

## 構成

| ファイル | 役割 |
|---|---|
| `caption.py` | キャプション生成と検証。`build_post()` は生成→検証→ログの順で必ず実行(バイパス不可) |
| `video_check.py` | 投稿前の動画ゲート。ffprobe で実寸を見て縦型以外を落とす。回転メタデータを考慮する |
| `tiktok_api.py` | TikTok Content Posting API クライアント (Direct Post / FILE_UPLOAD)。標準ライブラリのみ |
| `auth_setup.py` | 初回 OAuth。人間が踏むのはここ1回だけ |
| `post_video.py` | 端から端まで: ゲート → キャプション検証 → 投稿 → 完了待ち |
| `config/caption_rules.json` | 禁止ワード・禁止タグ・固定ハッシュタグ・上限値 |
| `config/post_rules.json` | 縦型必須・尺・fps・サイズ・公開範囲・ポーリング設定 |

設定値は**どちらも JSON 側だけ編集して更新する**。コードは触らない。

## 使い方

```bash
# 投稿せずゲートだけ通す (API を一切叩かない)
python3 -m tiktok_autopost.post_video --video /tmp/clip.mp4 \
    --caption "she said yes at the grove" --dry-run

# 実投稿
python3 -m tiktok_autopost.post_video --video /tmp/clip.mp4 \
    --caption "she said yes at the grove" \
    --first-comment "she almost said no" \
    --token-state token_state.json

# 動画ゲート単体
python3 -m tiktok_autopost.video_check /tmp/clip.mp4

# キャプション検証単体
python3 -m tiktok_autopost.caption --validate-only \
    --caption "she said yes at the grove" --first-comment "she almost said no"
```

`post_video` の終了コード: `0` 投稿完了 / `2` ゲート落ち(未投稿) / `1` API エラー。

## ゲート（1つでも落ちたら API を叩かずに停止）

**動画** — 縦型 (`height > width`)、短辺360px以上、3〜600秒、23〜60fps、1GB以下、mp4/mov/webm。
コンテナ上が横でも `rotate=90` 等が付いていれば表示上は縦なので通す。逆も落とす。

**キャプション** — 1行・全小文字・ピリオドなし・60文字以内(タグ除く)。
禁止ワード、禁止タグ、URL、本文内タグ、大文字3連続はエラー。
ハッシュタグは固定4つをこの順で: `#cansilkyourhair #hairoil #hairtransformation #silktherich`。
**検証違反は例外を投げて投稿を止める。黙って修正しない。**

## 審査待ちの間

審査を通していないアプリは、`PUBLIC_TO_EVERYONE` を指定しても TikTok 側で
**強制的に非公開**にされる。`post_video.py` は投稿前に `creator_info` を見て
選べない公開範囲なら投稿せずに止めるので、黙って非公開投稿される事故は起きない。

審査が降りるまでの選択肢:
- `config/post_rules.json` の `privacy_level` を `SELF_ONLY` にしてパイプライン全体を通す
  （デモ録画もこれで撮る）
- 公開投稿は手動で続ける

## 既知の制約

- **ファーストコメントは API で投稿できない。** Content Posting API にコメント作成の
  エンドポイントが無い。`--first-comment` は検証とログのみ。
  対応は (a) 手でコメントする (b) 本文に織り込む のどちらか。
- **リフレッシュトークンは更新ごとにローテーションする。** コンテナが使い捨てなので
  `token_state.json` を永続先(ドライブ)に書き戻さないと次回失敗する。
- **`open.tiktokapis.com` はこの環境のネットワークポリシーでブロックされている。**
  現状はサンドボックス経由。許可リストに入れれば直接叩ける。

## テスト

```bash
python3 -m tiktok_autopost.tests.test_caption      # 17件 キャプション規則
python3 -m tiktok_autopost.tests.test_posting      # 20件 チャンク分割・トークン保存・ゲート
python3 -m tiktok_autopost.tests.test_video_probe  # 25件 向き判定・回転補正・fps/尺/サイズ
```

`test_video_probe` は ffprobe の JSON を差し込むので ffprobe 無しでも走る。
`test_posting` の一部は ffmpeg がある環境でのみ実行され、無ければスキップする。

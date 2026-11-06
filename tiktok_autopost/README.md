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
| `transcode.py` | ゲート落ちのうち**解像度/fps/容量だけ**が理由のものを再エンコードして通す。横動画と尺超過は変換しない |
| `media_host.py` | 変換した動画を Late(Zernio) の保管に上げて公開URLを返す。署名付きURLへの PUT には Authorization を付けない |
| `post_video.py` | 端から端まで: ゲート → (必要なら変換) → キャプション検証 → 投稿 → 完了待ち |
| `config/caption_rules.json` | 禁止ワード・禁止タグ・固定ハッシュタグ・上限値 |
| `config/post_rules.json` | 縦型必須・尺・fps・サイズ・自動変換・公開範囲・ポーリング設定 |

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

## 自動変換 (サイズ・解像度)

Drive には 8K60 HEVC のような「中身は問題ないが TikTok が受け取れない」
素材が混ざる。これを人が毎回手で変換していると投稿が止まるので、
**再エンコードで直せる却下理由だけ**自動で潰す。

| 却下理由 | 挙動 |
|---|---|
| 長辺が 4096px 超過 | 長辺 1920px まで縮小 (アスペクト比は維持) |
| 短辺が 360px 未満 | 短辺 360px まで拡大 |
| fps が 23-60 の範囲外 | 30fps に合わせる |
| ファイルサイズ超過 | CRF で再エンコード → まだ超えるならビットレート指定で詰め直す (最大3回) |
| **縦型ではない** | **変換しない。** クロップやパディングで縦枠に入れれば「投稿できる」状態にはなるが、「横動画は投稿しない」は画角の要件であってファイル形式の話ではない |
| **尺が範囲外** | **変換しない。** 10分超を切るのは編集判断 |
| 上記以外の未知の理由 | **変換しない。** 黙って投稿するより止めて人に見せる |

直せない理由が 1 つでも混ざっていたら、直せる理由が同時にあっても
**ffmpeg は一度も走らない**。元ファイルは書き換えず、`<元の名前>.tiktok.mp4`
を別に書き出す。

### 変換した動画をどこに置くか

変換結果は Drive 上に存在しないので、Late に渡す公開URLをどこかに作る必要が
ある。これは **Late(Zernio) 自身のメディア保管**を使う(`media_host.py`)。

当初これを Higgsfield の CDN でやっていたが、Higgsfield が落ちると
**変換が必要な動画は投稿そのものができなくなる**。投稿経路の単一障害点を、
投稿に無関係なサービスに置いていた。Late 自身に署名付きアップロードが
あるので、保管先と投稿先を同じサービスに寄せた。これで Higgsfield は
「内容把握(キャプションの材料)」だけの依存に下がり、そこが落ちても
キャプションを人が書けば投稿はできる。

手順(2026-11-06に実地で確認):

```
POST /api/v1/media {filename, contentType}
    -> {uploadUrl(署名付きPUT, 1時間有効), publicUrl, key, type}
PUT uploadUrl   本文=動画。**Authorization を付けてはいけない**(署名が壊れる)
HEAD publicUrl  取得できてサイズが合うことを必ず確認する
```

最後の確認を省くと、404 を返すURLを Late に渡して投稿が静かに失敗する。
実測で 115MB が約7秒。

```bash
# 変換だけ試す
python3 -m tiktok_autopost.transcode /tmp/8k.mp4 --workdir /tmp/out

# 変換して公開URLまで作る (投稿にはこの public_url を使う)
python3 -m tiktok_autopost.transcode /tmp/8k.mp4 --workdir /tmp/out --host

# 既にあるファイルを公開URLに載せるだけ
python3 -m tiktok_autopost.media_host /tmp/out/8k.tiktok.mp4

# 投稿フローに任せる (ゲート落ちしたら自動で変換される)
python3 -m tiktok_autopost.post_video --video /tmp/8k.mp4 \
    --caption "..." --first-comment "..." --workdir /tmp/out

# 自動変換を切る
python3 -m tiktok_autopost.post_video ... --no-transcode
# もしくは config/post_rules.json の "transcode_enabled": false
```

ffmpeg が無い環境では変換できないので、その旨を理由に入れて却下する
(無言で素材を投稿してしまうことはない)。

## テスト

```bash
python3 -m tiktok_autopost.tests.test_caption      # 17件 キャプション規則
python3 -m tiktok_autopost.tests.test_posting      # 33件 チャンク分割・トークン保存・ゲート
python3 -m tiktok_autopost.tests.test_video_probe  # 28件 向き判定・回転補正・fps/尺/サイズ
python3 -m tiktok_autopost.tests.test_mp4_probe    # 19件 ffprobe 無し環境の MP4 パーサ
python3 -m tiktok_autopost.tests.test_transcode    # 20件 何を変換し、何を変換しないか
python3 -m tiktok_autopost.tests.test_media_host   # 13件 署名付きアップロードの手順
```

`test_video_probe` は ffprobe の JSON を差し込むので ffprobe 無しでも走る。
`test_posting` の一部は ffmpeg がある環境でのみ実行され、無ければスキップする。

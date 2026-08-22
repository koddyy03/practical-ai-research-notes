# tiktok_autopost — "Can I Silk Your Hair?" キャプション生成・検証

街頭シリーズのTikTok自動投稿用。広告に見えた時点で数字が死ぬので、
**商品を売る言葉を1つでも出さない**ことをコードで強制する。

## 構成

| ファイル | 役割 |
|---|---|
| `caption.py` | 生成 (`generate_caption` / `generate_first_comment`) と検証 (`validate_caption` / `validate_first_comment`)。`build_post()` は生成→検証→ログの順で必ず実行(バイパス不可) |
| `config/caption_rules.json` | 禁止ワード・禁止タグ・固定ハッシュタグ・上限値。**コードを触らずにここだけ編集して更新する** |
| `tests/test_caption.py` | `python -m tiktok_autopost.tests.test_caption` で実行 |

## 使い方

```bash
# 生成 + 検証 + ログ出力
python -m tiktok_autopost.caption \
    --location "The Grove" --subject-note "pink hair" --outcome "she said yes"

# 外部(LLM等)で生成したキャプションの検証のみ。違反なら exit 1
python -m tiktok_autopost.caption --validate-only \
    --caption "she said yes at the grove" --first-comment "she almost said no"
```

## ルール要約

- キャプションは1行・全小文字・ピリオドなし・60文字以内(タグ除く)。動画内で起きたことだけ
- 検索用語 `hair oil / frizzy hair / hair before after / hair transformation` を自然に含められる場合のみ含める(努力目標)
- ハッシュタグは固定4つをこの順で: `#cansilkyourhair #hairoil #hairtransformation #silktherich`
- 禁止ワード(部分一致・大文字小文字無視)、禁止タグ(#fyp等)、URL、本文内タグ、大文字3連続はエラー
- ファーストコメントは動画の外側の情報を1行。リンク・商品名・購入導線はエラー
- **検証違反は例外を投げて投稿を止める。黙って修正しない**

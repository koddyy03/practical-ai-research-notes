# 審査申請の文面（コピペ用）

TikTok 開発者コンソールの Content Posting API 審査フォームに貼る内容。
`<>` の箇所だけ埋める。

---

## App name

```
Silk The Rich Studio Poster
```

## App description

```
An internal publishing tool for Silk The Rich, a Japanese premium hair care
brand operating in the United States. Our team films a recurring street
interview series on TikTok and stores the finished vertical clips in our
company Google Drive. This application moves an approved clip from that
Drive folder to our own TikTok account on a fixed weekly schedule, so that
our small team does not have to publish each clip by hand.

The application serves exactly one TikTok account: our founder's own
account, which he uses to publish this series for the brand. It is not a
product offered to other users, it does not accept sign-ups, and it never
posts on behalf of anyone else. The account holder is the person who
authorizes the application, so the app only ever posts to the account of
the person who granted it access.
```

## Which scopes do you need, and why?

```
user.info.basic
  Required to call the creator info endpoint before each post. We use the
  returned privacy_level_options and max_video_post_duration_sec to confirm
  that the post settings we are about to send are valid for the account.

video.publish
  Required to publish an approved vertical video directly to the account's
  profile with a caption. Direct Post is what removes the manual step; the
  purpose of the integration is to remove that step.

We request no other scopes. We do not read analytics, do not read other
users' content, and do not need any permission beyond publishing to our own
account.
```

## Describe your use case in detail

```
1. A finished vertical clip is placed in a designated Google Drive folder by
   our team.
2. On a fixed schedule (Tuesday, Thursday and Saturday at 19:00 JST), our
   automation takes the oldest unposted clip from that folder.
3. The clip is validated locally before any API call: it must be portrait
   orientation, 3 to 600 seconds, at least 360 pixels on the short side,
   23 to 60 FPS, and under 1 GB. Anything that fails is skipped and reported
   to our founder instead of being posted.
4. A caption is generated and validated against a fixed internal rule set,
   then the clip is published to our own account via the Content Posting API
   using FILE_UPLOAD and Direct Post.
5. We poll the publish status endpoint until PUBLISH_COMPLETE and record the
   result.

All content is filmed and owned by our company. Every person who appears in
the series is asked on camera for permission before filming, and we do not
publish footage of anyone who declines. We publish only our own original
footage and never re-upload third-party content.
```

## Volume of requests

```
Three posts per week, one video per run. In practice fewer than 15 publish
calls per month, plus one creator info call and one token refresh per run.
We are well under the documented rate limits and have no plans to increase
the volume.
```

## Demo video (審査で必須)

審査では「実際に動いている様子」の録画提出が求められる。以下を1本撮って出す:

1. ドライブのフォルダに動画が入っている画面
2. 投稿処理を実行しているところ（ログに縦型判定とキャプション検証が出る）
3. TikTok のプロフィールに実際に投稿が出ている画面

審査が降りる前は公開投稿ができないため、デモは **非公開 (SELF_ONLY)** の
投稿で撮ることになる。そのことを申請文に一言添えておくと差し戻されにくい:

```
Note: this demo was recorded with privacy_level SELF_ONLY, because an
unaudited client cannot post publicly. The production configuration uses
PUBLIC_TO_EVERYONE and is otherwise identical.
```

## 必要な公開ページ（審査で確認される）

| 項目 | URL | 状態 |
|------|-----|------|
| Terms of Service | `https://silktherich.com/terms` | <要確認> |
| Privacy Policy | `https://silktherich.com/privacy` | <要確認> |
| Redirect URI | `https://silktherich.com/tiktok-callback` | <要作成> |

この3つが実在して開けないと審査で落ちる。Shopify のストアに
ページが無ければ先に作る必要がある。

## 審査でよく落ちる点

- **スコープの過剰要求** — 使わないスコープを足さない。上記2つだけにする。
- **デモが不十分** — 投稿が実際に TikTok 上に出ているところまで映す。
- **ポリシーページが無い / 404** — 上の表の3つを先に用意する。
- **用途が「他社向けツール」に見える** — 自社1アカウント専用だと明記する（上の文面はそう書いてある）。
- **承認後の仕様変更** — 変更すると再審査になるので、申請前に仕様を固める。

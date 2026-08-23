# ✌️ Duo Blog

둘이서(또는 가족·소그룹이) 같이 쓰는 **비공개 셀프호스팅 블로그** 템플릿.

- 화면은 [fuwari](https://github.com/saicaca/fuwari)(Astro 정적 블로그) 그대로 — 다크모드·목차·검색까지 예쁘게 나온다.
- 블로그를 보다가 오른쪽 아래 **✏️(이 글 수정) / ＋(새 글)** 버튼으로 바로 쓴다. 왼쪽 마크다운 편집기, 오른쪽엔 블로그 스타일 그대로의 실시간 미리보기.
- 서버는 `server/server.py` **파이썬 표준 라이브러리 단일 파일**. 데이터베이스도, 외부 서비스도, 클라우드 계정도 필요 없다.
- 로그인은 자체 계정(이메일+비밀번호). **가입 페이지가 없다** — 계정은 관리자가 명령 한 줄로 만든다. 우리끼리 쓰는 물건이라는 뜻이다.
- [Tailscale](https://tailscale.com)로 묶으면 인터넷에 아무것도 노출하지 않고 두 사람 기기에서만 열린다.
- 글을 저장할 때마다 자동으로 git 커밋(+원격이 있으면 push)되어 백업이 남는다.

## 왜 만들었나

둘이만 쓰는 블로그를 만들면서 Cloudflare Access + Git 기반 CMS로 먼저 갔다가, "글 한 번 쓰는데
이메일 인증코드에 GitHub OAuth까지" 통과해야 하는 게 두 사람 규모에는 과하다는 걸 배웠다.
필요한 건 **내 서버에서 도는 작은 프로세스 하나**였다.

## 빠른 시작

요구사항: Python 3.9+, Node.js 20+ (pnpm)

```sh
git clone https://github.com/000namc/duo-blog.git my-blog
cd my-blog
pnpm install && pnpm build        # 정적 사이트 빌드 (dist/)

python3 server/server.py adduser you@example.com 이름
# → 임시 비밀번호가 출력된다. 함께 쓸 사람 계정도 같은 방식으로.

python3 server/server.py           # http://127.0.0.1:4600
```

브라우저에서 열고 로그인하면 끝. 글을 저장하면 자동으로 재빌드된다(수 초).
비밀번호는 로그인 후 `/account`에서 각자 바꾼다.

## 서버에 올리기 (상시 운영)

집에 남는 맥, 개인 맥북, 무료 클라우드 VM(Oracle) 어디든 좋다.
**Tailscale 세팅·상시 구동(launchd/systemd)·비공개 HTTPS 노출**은 별도 가이드로 정리해뒀다:

👉 **[selfhost-kit — 나의 첫 셀프호스팅 서버 만들기](https://github.com/000namc/selfhost-kit)**

요지만 적으면:

```sh
# tailnet 전용 HTTPS (Tailscale 켠 기기에서만 접속됨)
tailscale serve --bg --https=10000 http://127.0.0.1:4600
```

## 구조

| 파일 | 역할 |
|---|---|
| `server/server.py` | dist/ 서빙(로그인 필수) + ✏️ 편집 + 저장 시 `pnpm build` + git 백업 |
| `server/users.json` | 계정(PBKDF2 해시). git에 올라가지 않음 |
| `src/content/posts/*.md` | 글 원본 (frontmatter: title, published, tags, category, draft) |
| `src/config.ts` | 블로그 제목·색·프로필 등 fuwari 설정 |

## 커스터마이즈

- 블로그 제목·색·프로필: `src/config.ts`
- 데모 글 삭제: `src/content/posts/`에서 지우고 다시 빌드
- 세션 기간·포트: `server/server.py` 상단 상수 (`DUO_BLOG_PORT` 환경변수로도 지정 가능)

## 라이선스와 출처

화면 전체는 [fuwari](https://github.com/saicaca/fuwari)(MIT, [LICENSE](LICENSE))의
2026-08 시점 스냅샷이고, 여기에 서버 한 파일을 더했다. 추가분도 MIT.
fuwari의 새 기능이 필요하면 업스트림에서 받아 수동으로 반영한다.

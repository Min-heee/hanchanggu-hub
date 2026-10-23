#!/bin/sh
# 빈자리(전화 예약 빈 시간 도구, 별도 저장소)를 한창구의 /binjari/ 아래에서 데모성형외과 규칙·CRM 예약으로 돌게 빌드해 vendor/binjari에 넣는다.
#
# - 원본 저장소의 작업 폴더는 건드리지 않는다: 정한 브랜치(커밋)를 git archive로 임시 폴더에 풀어 그 사본으로 빌드한다.
# - 규칙 문서(볼트)는 scripts/make_binjari_vault.py가 한창구 안내 자료로 만든 데모성형외과 것으로 바꾸고,
#   샘플의원 합성 예약은 비운다. 기존 예약과 기준 시각은 화면을 열 때 한창구의 /binjari/feed.js에서 받는다.
# - 빈자리의 '다른 앱에 품기' 빌드(BINJARI_BASE_PATH, NEXT_PUBLIC_BINJARI_FEED=1)를 쓴다.
#
# 사용: scripts/vendor_binjari.sh [빈자리 저장소 경로] [브랜치·커밋]
#   기본: ~/.openclaw/workspace/binjari, feat/hanchanggu-feed (원본에 npm install이 돼 있어야 함)
set -eu
SRC_ARG=${1:-"$HOME/.openclaw/workspace/binjari"}
SRC=$(cd "$SRC_ARG" 2>/dev/null && pwd -P) || { echo "빈자리 저장소 경로를 찾을 수 없어요: $SRC_ARG" >&2; exit 1; }  # 상대 경로도 절대 경로로
REF=${2:-${BINJARI_REF:-feat/hanchanggu-feed}}
HERE=$(cd "$(dirname "$0")/.." && pwd -P)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP" "$TMP.tar"' EXIT

[ -d "$SRC/node_modules" ] || { echo "빈자리 저장소에서 먼저 npm install 하세요: $SRC" >&2; exit 1; }
# 빌드하는 동안 브랜치가 움직여도 기록과 빌드가 같은 커밋이 되도록 커밋을 고정한다
COMMIT=$(git -C "$SRC" rev-parse --verify --quiet "$REF^{commit}") || { echo "빈자리 저장소에 $REF가 없어요." >&2; exit 1; }
git -C "$SRC" archive -o "$TMP.tar" "$COMMIT"  # 파이프로 풀면 archive 실패를 놓칠 수 있어 파일로 받는다
tar -x -C "$TMP" -f "$TMP.tar"
rm -f "$TMP.tar"
grep -q "NEXT_PUBLIC_BINJARI_FEED" "$TMP/src/demo/feed.ts" 2>/dev/null || { echo "$REF에는 '다른 앱에 품기'(feed.js) 기능이 없어요." >&2; exit 1; }
ln -s "$SRC/node_modules" "$TMP/node_modules"

python3 -I "$HERE/scripts/make_binjari_vault.py" "$TMP" "$TMP/vault"
printf '{"meta": {"fictional": true, "note": "한창구 품기 빌드: 기존 예약은 /binjari/feed.js에서 받는다"}, "bookings": [], "exceptions": []}\n' > "$TMP/data/bookings.json"

(cd "$TMP" && BINJARI_BASE_PATH=/binjari NEXT_PUBLIC_BINJARI_FEED=1 npm run build)
# 빈자리는 규칙 문서가 틀리면 빌드는 되지만 규칙 화면에 '규칙 오류'를 그린다. 그런 빌드는 넣지 않는다.
if grep -q "규칙 오류" "$TMP/out/rules/index.html"; then
  echo "빈자리가 데모성형외과 규칙 문서를 거부했어요. 빌드를 넣지 않았어요 ($TMP/out/rules/index.html의 오류 목록 확인)." >&2
  trap - EXIT
  exit 1
fi
rm -rf "$HERE/vendor/binjari"
mkdir -p "$HERE/vendor"
cp -R "$TMP/out" "$HERE/vendor/binjari"
echo "$REF $(git -C "$SRC" log -1 --format='%h %ad %s' --date=short "$COMMIT")" > "$HERE/vendor/binjari/SOURCE_COMMIT.txt"
echo "vendor/binjari 갱신 완료 ($(cat "$HERE/vendor/binjari/SOURCE_COMMIT.txt"))"

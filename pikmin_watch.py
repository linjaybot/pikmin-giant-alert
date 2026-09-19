#!/usr/bin/env python3
"""피크민 블룸 거대버섯 감시 봇 v5.1 (2026-09-13)

v5.1 수정(놓침·지연 사고 대응):
  - GO(전송) 버튼을 OCR로 즉시 찾음. 예전엔 매번 claude -p 비전 호출로 10~15초씩,
    4번 재시도해 50초를 버렸다(9/13 03:15:43~03:16:33). 이제 1~2초.
  - 카드 키를 장소명 정규화 + 접두사 비교로 판정. 같은 버섯이 말줄임 여부에 따라
    다른 키로 갈라져 3시간 방치되던 문제 수정.
  - 켤 때 화면에 보이는 거대 카드를 '아는 것'으로 등록하지 않음(재시작 한 번에 영구 무시됐음).
  - 카드 감지는 연속 2프레임 대기 없이 즉시 처리(6초 절약).
  - ⭐ 같은 자리 리스폰 대응: 장소는 버섯의 신원이 아니다(3시 참가 → 5시 소멸 → 6시 같은 자리
    재출현은 안 들어간 새 버섯). 참가 여부는 리스트 위치로 판정한다 — 참가한 카드는 첫 화면에
    오지 않고 맨 뒤로 밀리므로, '버섯 개수: 1/M' 화면에 보이는 거대는 미참가로 확정한다.
    known_cards는 신원 목록이 아니라 '최근 확인함' 단기 캐시로만 쓰고, 보일 때마다 갱신하지 않는다.


6초마다 iPhone 미러링 창을 캡처해서 감지:
  A) 맵에 새 거대버섯 등장 — 사이즈 기반 후보 검출(생김새 무관) 후
     템플릿 일치면 즉시, 아니면 claude -p 비전으로 최종 판정 (매달 스킨 바뀌어도 동작)
  B) 하단 버섯 리스트의 '거대' 카드 수 증가 (초대받은 원거리 거대 포함)
어느 쪽이든 2연속 관측되면 → 하단 카드 탭 → '참가' 버튼 탭으로 봇이 직접 참가(8/22).
참가 실패 시에만 기존 팝업+알람으로 폴백.

- 맵 화면(탐험 탭)일 때만 판정. 전투/홈 화면에선 쉼
- 아는 거대 아이콘은 위치가 밀려도 따라가며 갱신
사용법: 피크민 / 피크민 --reset
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import cv2
import numpy as np
import Quartz
import Vision

INTERVAL_SEC = 6             # 초대 푸시 배너(5~6초)도 잡아야 해서 타이트하게
INVITE_WORDS = ("초대",)      # 이 단어가 있는 줄을 초대 푸시 후보로 봄
INVITE_EXCLUDE = ("초대하기",)  # 단순 버튼 라벨(카톡 '초대하기' 등)은 제외 — 7/31 오탐 원인
INVITE_CONTEXT = ("버섯", "도전", "챌린지", "Pikmin", "피크민")  # 화면 어딘가에 함께 있어야 인정
INVITE_COOLDOWN = 180        # 초대 알림 재발사 최소 간격(초)
MATCH_THRESHOLD = 0.72
KNOWN_RADIUS = 80            # 아는 아이콘 추적 반경(px). 150은 맵 절반이 사각지대라 80으로 축소(8/15)
CANDIDATE_RADIUS = 60
CONFIRM_FRAMES = 2
# known_cards는 '이 버섯의 신원'이 아니라 '최근에 탭해서 확인했다'는 단기 캐시다.
# 장소는 신원이 될 수 없다 — 같은 자리에서 버섯이 계속 리스폰되기 때문(9/13 사용자 지적).
# 3시에 참가한 버섯이 5시에 사라지고 6시에 같은 자리에 새로 뜨면 그건 안 들어간 새 버섯인데,
# 장소 키로 판단하면 영원히 '아는 카드'가 되어 놓친다. 그래서 일정 시간이 지나면 다시 탭해서
# 참가자 줄의 '당신' 유무로 판정한다(당신 있으면 조용히 넘어가고, 없으면 새 출현이므로 참가).
# 다만 주 판정은 리스트 위치다: 참가한 카드는 첫 화면에 오지 않고 맨 뒤로 밀리므로
# '버섯 개수: 1/M'인 화면에 거대 카드가 보이면 그건 안 들어간 것으로 확정할 수 있다(parse_card_page).
FIRSTPAGE_RETRY_SEC = 120      # 첫 화면의 미참가 거대에 재시도하는 최소 간격(초). 꽉 찬 방 대비
KNOWN_CARD_RECHECK_SEC = 1800  # 보조 안전망: 카루셀이 앞으로 안 돌아와 위치 신호를 못 쓸 때의 재확인 간격
KNOWN_CARD_TTL = 86400         # 캐시 하드 만료(초)
LOC_KEY_LEN = 8              # 카드 키에 쓸 장소명 길이(정규화 후 앞 N자)
LOC_KEY_MIN = 4              # 키 접두사 비교 최소 길이 — 이보다 짧게 겹치면 다른 카드로 본다
CARD_LOC_DY = (55, 135)      # 카드 제목 아래 위치(•장소) 줄의 세로 거리 범위(px, 644폭 기준)
CARD_LOC_DX = 170            # 제목-장소 가로 중심 거리 허용치(카드 폭 ~400, 옆 카드는 ~390 떨어짐)
# 사이즈 기반 후보 검출 + LLM 최종 판정 (거대버섯 생김새가 매달 바뀌어도 동작)
GIANT_R_LO = 15              # 내접원 반지름(px, 644폭 기준) 이상이면 거대 후보
REJECT_RADIUS = 80           # LLM이 기각한 위치 재질의 억제 반경
LLM_COOLDOWN = 60            # LLM 판정 최소 간격(초)
LLM_TIMEOUT = 120
# 비전 판정이 CLUSTER(군집)라 애매할 때만 하단 카드 리스트를 끝까지 넘겨 거대 카드가 있는지 확인(사용자 요청 8/16)
SWEEP_ON = ("CLUSTER",)
SWEEP_COOLDOWN = 180         # 초. 리스트 넘기기 최소 간격
# ⏱️ 정기 전체 점검 주기(초). 9/19 사고의 직접 원인:
# 봇은 화면에 보이는 카드(카루셀 12장 중 1~2장)만 읽는다. 거대가 뒤쪽에 있으면 존재를 모른다.
# 그런데 전체를 훑는 sweep이 'LLM이 CLUSTER 판정했을 때'라는 조건부라, 16:41~17:39 58분 동안
# 한 번도 안 돌았고 그 사이 뜬 거대(큐브 정자)를 한참 뒤에야 발견했다. 이제 조건 없이 주기적으로 돈다.
SWEEP_INTERVAL_SEC = 180
SWEEP_MAX_SWIPES = 16
SWEEP_LOCK = os.path.join(os.path.expanduser("~/pikmin-watch"), "sweeping.lock")  # 넘기는 동안 복구 데몬이 마우스 안 건드리게
CLICLICK = "/opt/homebrew/bin/cliclick"
# 전투 애니메이션 등으로 한두 프레임 인식이 끊겨도 아는 것/기각 목록을 바로 지우지 않는다
# (8/2 재알람 사태 원인: 즉시 삭제 → 재등장 시 새 거대로 착각)
SPRITE_GRACE_SEC = 300
REJECT_GRACE_SEC = 600
CLAUDE_CLI = os.path.expanduser("~/.local/bin/claude")
MAP_REGION_RATIO = 0.46
REF_FRAME_WIDTH = 644
MAP_MARKERS = ("탐험", "모종", "엽서", "라이프")
# 미러링 끊김/일시정지 화면에 뜨는 문구 (얼어붙은 맵 위 오버레이 포함)
DISCONNECT_WORDS = ("다시 연결", "연결이 끊", "iPhone 카메라", "iPhone이 잠", "사용 중이므로")
FROZEN_FRAMES = 3            # 이 횟수 연속 완전 동일 화면이면 멈춘 것으로 판정
WINDOW_OWNERS = ("iPhone Mirroring", "iPhone 미러링")
# 운영 시간: 한국시간 토 00:00 ~ 월 00:00 (48시간). 평일엔 화면이 떠 있어도 감시 안 함
KST = ZoneInfo("Asia/Seoul")
ACTIVE_WEEKDAYS = (5, 6)     # 토=5, 일=6
# 하루 도전 횟수: "오늘은 앞으로 N회". 0회를 보면 그날 자정까지 알림 정지, 다음날 N>0을 봐야 재개
REMAIN_RE = re.compile(r"앞으로\s*(\d+)\s*회")

# 자동 참가 (8/22 사용자 요청): 새 거대를 알림 대신 봇이 직접 참가. 실패 시에만 알림 폴백.
# 흐름: 하단 리스트에서 해당 '거대' 카드 탭 → 상세 화면의 '참가' 버튼 탭. 실기 검증 전 — 단계 스크린샷을 joins/에 남김
AUTO_JOIN = True
JOIN_BUTTON_TEXTS = ("참가", "참가하기", "참여")
# 피크민 선택 화면 우하단의 전송 버튼. 현재 스킨은 동그란 'GO' 버튼(9/13 실측 (556,1275) 부근)
GO_BUTTON_TEXTS = ("GO", "GO!", "전송", "확정", "보내기")
GO_BUTTON_XY = (560, 1285)   # OCR이 GO를 못 읽을 때 바로 쓰는 고정 좌표(644폭 기준)
# ⛔ 유한한 자원을 소모하는 확인 팝업. 이 단어가 보이면 절대 승인하지 않고 취소한다.
# 9/19 사고: 5명이 찬 방은 티켓이 있어야 들어갈 수 있는데, "티켓을 사용해서 참가하시겠습니까?"
# 팝업의 OK를 봇이 눌러 사용자 티켓을 1장(소지 수 10→9) 말없이 소모했다.
COST_CONFIRM_WORDS = ("티켓", "소지 수", "구매", "결제", "코인", "유료", "루비", "젬")
CANCEL_TEXTS = ("취소", "아니오", "닫기", "Cancel")
JOIN_MAX_SWIPES = 16
# 맵 아이콘을 탭해서 열린 상세가 이 문구를 포함하면 버섯이 아님(과일 탐험 등) → 오탐 기각 (8/23 레몬 사건)
NOT_GIANT_MARKERS = ("탐험으로", "피크민을 탐험에", "발견한 날")

BASE = os.path.expanduser("~/pikmin-watch")
TEMPLATE_DIR = os.path.join(BASE, "templates")
JOIN_DIR = os.path.join(BASE, "joins")
STATE_PATH = os.path.join(BASE, "state.json")
SNAP_DIR = os.path.join(BASE, "hits")
LOG_PATH = os.path.join(BASE, "watch.log")
HB_PATH = os.path.join(BASE, "heartbeat.json")
FRAME_DIR = os.path.join(BASE, "frames")   # 진단용 프레임 링버퍼(놓친 거대 사후 확인용)
FRAME_EVERY = 30                            # 초
FRAME_KEEP = 240                            # 장 (=2시간)


def now_kst():
    return datetime.now(KST)


def in_service_window():
    return now_kst().weekday() in ACTIVE_WEEKDAYS


def parse_remaining(texts):
    """'오늘은 앞으로 N회' → N, 없으면 None"""
    for t in texts:
        m = REMAIN_RE.search(t)
        if m:
            return int(m.group(1))
    return None


def update_quota(state, texts):
    """횟수 표시를 보고 quiet_until(그날 자정) / armed_day(N>0 확인한 날짜) 갱신. 변경 있으면 True"""
    n = parse_remaining(texts)
    if n is None:
        return False
    now = now_kst()
    today = now.strftime("%Y-%m-%d")
    changed = False
    if n == 0:
        midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        if state.get("quiet_until", 0) < midnight.timestamp():
            state["quiet_until"] = midnight.timestamp()
            log(f"오늘 도전 횟수 소진(0회) → {midnight.strftime('%m/%d 00:00')}까지 알림 정지")
            changed = True
    else:
        if state.get("armed_day") != today:
            state["armed_day"] = today
            log(f"오늘 남은 도전 {n}회 확인 → 알림 활성 ({today})")
            changed = True
    return changed


def alert_state(state):
    """'ok' | 'quiet'(횟수 소진, 자정까지) | 'not_armed'(오늘 N>0 아직 못 봄)"""
    now = now_kst()
    if state.get("quiet_until", 0) > now.timestamp():
        return "quiet"
    if state.get("armed_day") != now.strftime("%Y-%m-%d"):
        return "not_armed"
    return "ok"


def heartbeat(status):
    """상단바 앱이 읽는 심박. status: watching/not_map/no_window/alert"""
    try:
        with open(HB_PATH, "w") as f:
            json.dump({"ts": time.time(), "status": status}, f)
    except Exception:
        pass


def log(msg):
    line = f"[{datetime.now().strftime('%m/%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def find_mirror_window():
    wins = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID
    )
    for w in wins:
        if w.get("kCGWindowOwnerName") in WINDOW_OWNERS:
            b = w.get("kCGWindowBounds")
            if b and b["Width"] > 200:
                return w.get("kCGWindowNumber"), b
    return None


def capture_window(win_id, path):
    r = subprocess.run(
        ["screencapture", "-x", "-o", "-l", str(win_id), path],
        capture_output=True,
    )
    return r.returncode == 0 and os.path.exists(path)


def ocr_boxes(image_path):
    """[(text, cx, cy)] — 644폭 기준 픽셀 좌표(좌상단 원점). 카드 제목↔장소 매칭에 사용"""
    url = Quartz.CFURLCreateWithFileSystemPath(
        None, image_path, Quartz.kCFURLPOSIXPathStyle, False
    )
    src = Quartz.CGImageSourceCreateWithURL(url, None)
    if src is None:
        return []
    img = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
    if img is None:
        return []
    W = Quartz.CGImageGetWidth(img)
    H = Quartz.CGImageGetHeight(img)
    scale = REF_FRAME_WIDTH / W if W else 1.0
    results = []

    def handler(request, error):
        if error is None and request.results():
            for obs in request.results():
                cand = obs.topCandidates_(1)
                if cand and len(cand):
                    bb = obs.boundingBox()
                    cx = (bb.origin.x + bb.size.width / 2) * W * scale
                    cy = (1 - (bb.origin.y + bb.size.height / 2)) * H * scale
                    results.append((str(cand[0].string()), cx, cy))

    request = Vision.VNRecognizeTextRequest.alloc().initWithCompletionHandler_(handler)
    request.setRecognitionLanguages_(["ko-KR", "en-US"])
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    h = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(img, None)
    h.performRequests_error_([request], None)
    return results


def ocr_texts(image_path):
    return [t for t, _, _ in ocr_boxes(image_path)]


def giant_card_boxes(boxes):
    """하단 버섯 리스트의 '거대' 카드를 [(키, 표시명, 제목중심x, 제목중심y)]로 수집.

    키 = '거대' + '|' + 장소 앞 6자. 거대버섯 이름은 그 달 스킨으로 다 똑같아서
    ('거대 바다거품 버섯' 등) 제목만 기억하면 같은 날 두 번째 거대부터 전부 놓친다(8/15 미탐 원인).
    장소 줄(• 장소명)은 제목 바로 아래 같은 카드 안에 있으므로 세로 55~135px, 가로 170px 안에서 찾는다.
    좌표는 644폭 기준 — 카드 탭(자동 참가)에 쓴다.
    """
    out = []
    for t, cx, cy in boxes:
        title = t.strip()
        if not title.startswith("거대"):
            continue
        loc, best = None, None
        for t2, cx2, cy2 in boxes:
            dy = cy2 - cy
            if CARD_LOC_DY[0] <= dy <= CARD_LOC_DY[1] and abs(cx2 - cx) < CARD_LOC_DX:
                cand = t2.strip().lstrip("•·・●.oOe0 ").strip()
                if not cand or cand[0].isdigit():      # HP 숫자 줄 등은 제외
                    continue
                d = abs(cx2 - cx)
                if best is None or d < best:
                    loc, best = cand, d
        if not loc:
            continue        # 장소를 못 읽은 카드(오른쪽 끝에 걸쳐 잘린 경우 등)는 이번 프레임에선 판단 보류
        # 키는 장소 위주. 제목은 오른쪽 끝에 걸치면 '거대 바다거'/'거대 바다거품 버'처럼 잘리는 길이가 달라져
        # 같은 카드가 두 번 울림(8/16 12:33·12:35). 거대버섯 이름은 그 달 내내 동일하므로 '거대'만 쓴다.
        out.append((f"거대|{norm_loc(loc)}", f"{title} @ {loc}", cx, cy))
    return out


def norm_loc(loc):
    """장소명을 키로 정규화. 말줄임/공백/문장부호 제거 후 앞 LOC_KEY_LEN자.

    9/13 미탐 원인: 같은 버섯인데 카드 폭에 따라 '황제팽귄의...'(말줄임)와
    '황제팽귄의 얼음낚시놀이터'(전체)로 읽혀 키가 '거대|황제팽귄의.' / '거대|황제팽귄의 '
    로 갈라짐 → 하나는 아는 카드, 하나는 새 카드가 되어 3시간 동안 무시됐다.
    """
    s = re.sub(r"[\s.·・…]+", "", loc)       # 공백·마침표·말줄임 전부 제거
    return s[:LOC_KEY_LEN]


def same_card(a, b):
    """두 카드 키가 같은 버섯인지. OCR 절단 길이가 달라도 접두사가 겹치면 같은 것으로 본다."""
    if a == b:
        return True
    x, y = a.split("|", 1)[-1], b.split("|", 1)[-1]
    if not x or not y:
        return False
    n = min(len(x), len(y))
    return n >= LOC_KEY_MIN and x[:n] == y[:n]


def known_card_key(known_cards, key):
    """known_cards에서 같은 버섯으로 볼 수 있는 저장된 키를 반환(없으면 None).

    같은 버섯의 옛 변형 키가 여럿 남아 있을 수 있으므로 '가장 최근에 확인한' 키를 돌려준다.
    (오래된 변형이 먼저 잡히면 방금 확인했는데도 재확인 주기가 지난 것처럼 보여 무한 재확인이 된다)
    """
    if not known_cards:
        return None
    hits = [k for k in known_cards if same_card(k, key)]
    if not hits:
        return None
    return max(hits, key=lambda k: known_cards[k])


def card_keys(boxes):
    """{키: 표시명} — giant_card_boxes 참조"""
    return {k: disp for k, disp, _, _ in giant_card_boxes(boxes)}


def parse_card_page(texts):
    """하단 리스트의 '버섯 개수: N/M' 인디케이터 → (N, M). 못 읽으면 None.

    9/13 사용자 제공 정보: 내가 이미 참가한 카드는 리스트 첫 화면에 절대 안 오고 항상 맨 뒤로 밀린다.
    따라서 N==1(첫 화면)에 보이는 거대 카드는 '내가 안 들어간 것'으로 확정할 수 있다.
    실측으로 확인: 미참가 거대가 보일 때 '버섯 개수: 1/11', 참가 중 거대가 보일 때 '버섯 개수: 11/ 11'.
    이 신호가 있으면 카드를 탭해서 '당신'을 확인하지 않고도 리스폰 여부를 판정할 수 있다.
    """
    for t in texts:
        if "개수" not in t:
            continue
        m = re.search(r"(\d+)\s*/\s*(\d+)", t)
        if m:
            return int(m.group(1)), int(m.group(2))
    return None


def load_templates():
    tpls = []
    for name in sorted(os.listdir(TEMPLATE_DIR)):
        if name.endswith(".png"):
            t = cv2.imread(os.path.join(TEMPLATE_DIR, name))
            if t is not None:
                tpls.append((name, t))
    return tpls


def find_giants(frame_path, templates):
    img = cv2.imread(frame_path)
    if img is None:
        return []
    scale = REF_FRAME_WIDTH / img.shape[1]
    if abs(scale - 1.0) > 0.01:
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    map_img = img[: int(img.shape[0] * MAP_REGION_RATIO), :]

    found = []
    for name, tpl in templates:
        res = cv2.matchTemplate(map_img, tpl, cv2.TM_CCOEFF_NORMED)
        ys, xs = np.where(res >= MATCH_THRESHOLD)
        for x, y in zip(xs, ys):
            cx, cy = int(x + tpl.shape[1] / 2), int(y + tpl.shape[0] / 2)
            score = float(res[y, x])
            for f in found:
                if abs(f["x"] - cx) < CANDIDATE_RADIUS and abs(f["y"] - cy) < CANDIDATE_RADIUS:
                    if score > f["score"]:
                        f.update(x=cx, y=cy, score=score, tpl=name)
                    break
            else:
                found.append({"x": cx, "y": cy, "score": score, "tpl": name})
    return found


def _scaled_frame(frame_path):
    img = cv2.imread(frame_path)
    if img is None:
        return None
    scale = REF_FRAME_WIDTH / img.shape[1]
    if abs(scale - 1.0) > 0.01:
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    return img


def find_size_candidates(frame_path):
    """생김새 무관 거대 후보 검출: 선명한 색 덩어리의 최대 내접원 반지름이 큰 지점.

    맵 배경 초록(H 40~75)을 제외한 마스크 → 클로징(무늬 구멍 메움) → 거리변환 →
    국소 최대점 중 반지름 GIANT_R_LO 이상. 소형 군집도 걸리지만 LLM이 걸러낸다.
    """
    img = _scaled_frame(frame_path)
    if img is None:
        return []
    m = img[: int(img.shape[0] * MAP_REGION_RATIO), :]
    hsv = cv2.cvtColor(m, cv2.COLOR_BGR2HSV)
    h, s, v = cv2.split(hsv)
    mask = ((s > 100) & (v > 100) & ~((h >= 40) & (h <= 75))).astype(np.uint8) * 255
    closed = cv2.morphologyEx(
        mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9)))
    dist = cv2.distanceTransform((closed > 0).astype(np.uint8), cv2.DIST_L2, 5)
    dil = cv2.dilate(dist, np.ones((41, 41), np.uint8))
    ys, xs = np.where((dist >= GIANT_R_LO) & (dist >= dil - 1e-3))
    out = []
    for x, y in zip(xs, ys):
        for f in out:
            if abs(f["x"] - x) < 50 and abs(f["y"] - y) < 50:
                break
        else:
            out.append({"x": int(x), "y": int(y), "score": float(dist[y, x]), "tpl": "size"})
    return out


def llm_is_giant(frame_path, x, y):
    """후보 주변을 잘라 claude -p 비전으로 거대버섯 여부 최종 판정. True/False/None(실패)."""
    img = _scaled_frame(frame_path)
    if img is None:
        return None
    half = 85
    y0, y1 = max(0, y - half), min(img.shape[0], y + half)
    x0, x1 = max(0, x - half), min(img.shape[1], x + half)
    cv2.imwrite(os.path.join(BASE, "llm_check.png"), img[y0:y1, x0:x1])
    cv2.imwrite(os.path.join(BASE, "llm_check_full.png"),
                img[: int(img.shape[0] * MAP_REGION_RATIO), :])
    prompt = (
        "두 이미지를 Read 도구로 읽어라: ./llm_check_full.png (피크민 블룸 맵 전체 화면)과 "
        "./llm_check.png (그중 판정 대상을 확대한 크롭). 크롭 중앙의 대상을 다음 중 하나로 분류하라:\n"
        "- GIANT: 거대버섯. 버섯 갓 하나가 맵의 다른 어떤 버섯보다 압도적으로 커서 소형 버섯의 "
        "3배 이상이고, 도로 교차로 하나를 덮을 정도의 크기.\n"
        "- MEDIUM: 중형 버섯. 소형보다 크지만 2배 안팎 수준.\n"
        "- CLUSTER: 소형·중형 버섯 여러 개가 모여 있는 군집.\n"
        "- OTHER: 버섯이 아닌 것 (플레이어/다른 유저 아바타, 장식, 모종 화분, 피크민, "
        "그리고 레몬·사과·배 등 대형 과일(탐험 오브젝트) 포함 — 과일은 아무리 커도 GIANT가 아님. 8/23 레몬 오탐).\n"
        "전체 화면의 다른 버섯들과 크기를 비교해서 판단하고, 확실하지 않으면 GIANT라고 답하지 마라. "
        "마지막 줄에 분류 단어 하나만 출력하라."
    )
    try:
        proc = subprocess.run(
            [CLAUDE_CLI, "-p", "--model", "sonnet", prompt],
            capture_output=True, text=True, timeout=LLM_TIMEOUT, cwd=BASE,
        )
        lines = [l.strip() for l in proc.stdout.splitlines() if l.strip()]
        if proc.returncode != 0 or not lines:
            log(f"LLM 판정 실패 rc={proc.returncode} err={proc.stderr[:120]}")
            return None
        verdict = lines[-1].upper()
        log(f"LLM 분류: ({x},{y}) → {verdict[:20]}")
        return verdict.startswith("GIANT"), verdict[:20]
    except Exception as e:
        log(f"LLM 판정 오류: {e}")
        return None


def list_pt(b, x644, y644):
    """644폭 기준 프레임 좌표 → 화면(마우스) 좌표. 미러링 창은 레티나 2배 캡처라 /2"""
    return int(b["X"] + x644 / 2), int(b["Y"] + y644 / 2)


def list_swipe(b, dist):
    """하단 카드 리스트를 손가락처럼 드래그(카드가 눌리지 않게 8단계로 끈다)"""
    x0, y0 = list_pt(b, 560 if dist < 0 else 90, 940)
    x1 = x0 + int(dist / 2)
    args = [CLICLICK, f"dd:{x0},{y0}"]
    for i in range(1, 9):
        args.append(f"dm:{int(x0 + (x1 - x0) * i / 8)},{y0}")
    args.append(f"du:{x1},{y0}")
    subprocess.run(args, capture_output=True)


def tap(b, x644, y644):
    """644폭 기준 좌표를 클릭(폰 터치)"""
    x, y = list_pt(b, x644, y644)
    subprocess.run([CLICLICK, f"c:{x},{y}"], capture_output=True)
    return x, y


def zero_in_map(b):
    """시작(재개) 직후 지도가 축소된 넓은 뷰로 남아있으면 마커가 뭉쳐 보여 오탐 원인이 됨.
    탐험 탭 하단 리스트의 첫 버섯 카드를 탭해 들어갔다가 뒤로가기만 하면 지도가 그 동네로
    영점조준됨(2026-09-12 사용자 확인·실기 테스트). 참가/자동/확정 버튼은 절대 건드리지 않음
    (좌표는 카드 보기 화면 기준: 카드 (245,950), 상세화면 뒤로가기 (72,1310)).
    실패해도 감시 자체엔 지장 없으므로 예외는 삼키고 로그만 남긴다."""
    try:
        tap(b, 245, 950)
        time.sleep(1.2)
        tap(b, 72, 1310)
        time.sleep(1.0)
        log("영점조준: 첫 버섯 카드 탭 → 뒤로가기 완료")
    except Exception as e:
        log(f"영점조준 실패(무시): {e}")


def sweep_cards(win):
    """하단 버섯 리스트를 손가락 스와이프처럼 끝까지 넘기며 '거대' 카드를 모두 읽고 원위치. {키: 표시명}
    (마우스 드래그로 폰을 실제 터치함 — 카드가 눌리지 않게 380px씩, 8단계로 끈다. 12:45 실측 1회 25초+복귀 20초)"""
    wid, b = win
    def swipe(dist):
        list_swipe(b, dist)
    def read():
        f = "/tmp/pikmin_sweep.png"
        if not capture_window(wid, f):
            return None, {}
        bx = ocr_boxes(f)
        return [t for t, _, _ in bx if t.startswith(("거대", "중형", "소형"))], card_keys(bx)
    found, prev, n = {}, None, 0
    open(SWEEP_LOCK, "w").close()
    try:
        subprocess.run(["osascript", "-e", 'tell application "iPhone Mirroring" to activate'], capture_output=True)
        time.sleep(0.4)
        for _ in range(SWEEP_MAX_SWIPES):
            titles, keys = read()
            if titles is None:
                break
            found.update(keys)
            if titles == prev:
                break
            prev = titles
            swipe(-380); n += 1
            time.sleep(1.0)
        for _ in range(n + 2):
            swipe(+380); time.sleep(0.6)
    finally:
        try:
            os.remove(SWEEP_LOCK)
        except Exception:
            pass
    log(f"리스트 넘겨보기: 스와이프 {n}회, 거대 카드 {list(found.values())}")
    return found


def dist_ok(a, b, r):
    return abs(a["x"] - b["x"]) < r and abs(a["y"] - b["y"]) < r


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {"sprites": [], "rejected": [], "known_cards": {}}


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def alert_until_confirm(reason):
    log(f"알림 발사: {reason}")
    subprocess.run(["osascript", "-e", "set volume output volume 100"])
    dialog = subprocess.Popen([
        "osascript", "-e",
        'display dialog "🍄 새 거대버섯 나타남!" '
        'with title "거대버섯" buttons {"확인"} default button 1 '
        'with icon caution giving up after 60',
    ])
    while dialog.poll() is None:
        heartbeat("alert")
        subprocess.run(["afplay", "-v", "2", "/System/Library/Sounds/Sosumi.aiff"])
        time.sleep(0.2)


def join_snap(src, tag):
    """자동 참가 단계별 스크린샷 저장(실기 검증 전이라 사후 확인용)"""
    os.makedirs(JOIN_DIR, exist_ok=True)
    p = os.path.join(JOIN_DIR, datetime.now().strftime(f"j_%Y%m%d_%H%M%S_{tag}.png"))
    subprocess.run(["cp", src, p])
    return p


def llm_find_confirm(frame_path):
    """'보낼 피크민 선택' 화면에서 전송 확정 버튼 좌표를 LLM 비전으로 찾는다. (x,y) 644폭 기준, 실패 None"""
    img = _scaled_frame(frame_path)
    if img is None:
        return None
    cv2.imwrite(os.path.join(BASE, "llm_join.png"), img)
    prompt = (
        "이미지를 Read 도구로 읽어라: ./llm_join.png — 피크민 블룸의 '보낼 피크민 선택' 화면이다. "
        "선택을 확정하고 피크민을 버섯 도전에 보내는 버튼의 중심 좌표를 이 이미지의 픽셀 기준으로 찾아라. "
        "이 버튼은 피크민을 1마리 이상 선택한 뒤에야 나타난다(0/40이면 없음). "
        "버튼이 보이면 마지막 줄에 'x,y' 숫자만, 안 보이면 마지막 줄에 'NONE'만 출력하라."
    )
    try:
        proc = subprocess.run([CLAUDE_CLI, "-p", "--model", "sonnet", prompt],
                              capture_output=True, text=True, timeout=LLM_TIMEOUT, cwd=BASE)
        lines = [l.strip() for l in proc.stdout.splitlines() if l.strip()]
        m = re.match(r"^(\d+)\s*,\s*(\d+)$", lines[-1]) if lines else None
        if m:
            return int(m.group(1)), int(m.group(2))
    except Exception as e:
        log(f"확정 버튼 LLM 오류: {e}")
    return None


def finish_join(wid, b):
    """'참가' 탭 이후 이어지는 '보낼 피크민 선택(0/40)' 화면을 끝까지 처리(8/23 실전에서 발견).

    자동 버튼으로 40마리 선택 → 우하단 'GO' 버튼 탭 → 중간 확인 팝업이 있으면 눌러줌 →
    선택 화면과 '참가' 버튼이 모두 사라져야 성공 True.

    ⚡ 9/13 속도 수정: 예전엔 GO 좌표를 매번 claude -p(비전)로 찾아서 한 번에 10~15초씩 걸렸다.
    4번 재시도하며 50초를 버린 사고(9/13 03:15:43~03:16:33) 이후 순서를 뒤집음 —
    ① 이미 찍어둔 OCR 결과에서 'GO' 글자 찾기(추가 비용 0초)
    ② 없으면 우하단 고정 좌표 즉시 탭
    ③ 두 번 다 실패한 뒤에만 LLM 폴백
    (8/23 02:39·09:34·11:18 세 번 모두 이 화면에서 멈춰 참가가 안 된 사고의 재발 방지)"""
    f = "/tmp/pikmin_join.png"
    auto_tapped = confirm_tapped = False
    confirm_tries = 0
    for step in range(8):
        time.sleep(0.8)
        if not capture_window(wid, f):
            return False
        join_snap(f, f"fin{step}")
        bx = ocr_boxes(f)
        texts = [t.strip() for t, _, _ in bx]
        joined = " ".join(texts)
        if "피크민 선택" in joined:
            # OCR이 0을 'O'로 읽음: '(O/ 40)' — O/o 허용 후 0으로 치환 (8/23 실측)
            m = re.search(r"\(\s*([0-9Oo]+)\s*/\s*[0-9Oo]+\s*\)", joined)
            n_sel = int(m.group(1).replace("O", "0").replace("o", "0")) if m else None
            if not auto_tapped and (n_sel is None or n_sel == 0):
                btn = next(((x, y) for t, x, y in bx if t.strip() == "자동"), None)
                if btn:
                    log(f"참가: '자동' 선택 탭 ({int(btn[0])},{int(btn[1])})")
                    tap(b, btn[0], btn[1])
                    auto_tapped = True
                    continue
            # 선택된 상태(또는 자동 버튼 못 찾음) → GO 버튼 탭.
            # GO 버튼은 1마리 이상 선택 후에야 나타남(8/23 확인).
            confirm_tries += 1
            go = next(((x, y) for t, x, y in bx
                       if t.strip().upper() in GO_BUTTON_TEXTS), None)
            if go:
                xy, how = (int(go[0]), int(go[1])), "OCR"
            elif confirm_tries <= 2:
                xy, how = GO_BUTTON_XY, "고정좌표"
            else:
                xy = llm_find_confirm(f)          # 두 번 실패한 뒤에만 느린 LLM
                if xy is None:
                    log("참가: GO 버튼 못 찾음(OCR·고정·LLM 전부) → 재시도")
                    continue
                how = "LLM"
            log(f"참가: GO 버튼 탭 ({xy[0]},{xy[1]}) [{how}]")
            tap(b, xy[0], xy[1])
            confirm_tapped = True
            continue
        # ⛔ 자원(티켓 등)을 소모하는 확인 팝업은 절대 자동 승인하지 않는다.
        # 9/19 사고: 5명이 찬 방에 참가하려면 티켓이 필요한데, "티켓을 사용해서 참가하시겠습니까?"
        # 팝업의 OK를 이 코드가 눌러 사용자의 티켓 1장(10→9)을 말없이 써버렸다.
        # 이런 팝업은 취소를 누르고 참가를 포기한다(풀방은 어차피 티켓 없이 못 들어감).
        if any(w in joined for w in COST_CONFIRM_WORDS):
            cancel = next(((x, y) for t, x, y in bx if t.strip() in CANCEL_TEXTS), None)
            p = join_snap(f, "ticket_declined")
            if cancel:
                tap(b, cancel[0], cancel[1])
                log(f"참가 중단: 티켓 등 자원을 요구하는 팝업 → 취소 누름 snap={p}")
            else:
                log(f"참가 중단: 티켓 등 자원을 요구하는 팝업(취소 버튼 못 찾음) snap={p}")
            return "needs_ticket"
        ok = next(((x, y) for t, x, y in bx if t.strip() in ("확인", "OK", "네", "예")), None)
        if ok:
            log(f"참가: 확인 팝업 탭 ({int(ok[0])},{int(ok[1])})")
            tap(b, ok[0], ok[1])
            continue
        if not any(t in JOIN_BUTTON_TEXTS for t in texts):
            if confirm_tapped:
                p = join_snap(f, "done")
                log(f"참가: 선택 화면 종료 확인(자동 {auto_tapped}, 확정 {confirm_tapped}) snap={p}")
                return True
            # 선택 화면을 아직 못 봤는데 참가 버튼도 없음 → 로딩 중일 수 있으니 다음 스텝에서 재확인
            continue
    log("참가: 피크민 선택 화면을 끝내지 못함")
    join_snap(f, "fail")
    return False


def join_giant(win, prefer_keys=None, map_xy=None, known_cards=None):
    """새 거대버섯에 봇이 직접 참가: 하단 리스트의 해당 카드 탭 → 상세 화면의 '참가' 버튼 탭.

    prefer_keys: 이 키의 카드를 우선 탭(리스트 카드 감지 경로). 없으면 known_cards에 없는 거대 카드.
    카드가 안 보이면 리스트를 옆으로 넘기며 찾고, 그래도 없으면 map_xy(맵 아이콘)를 직접 탭.
    성공: 탭한 카드 키(문자열) 또는 True(맵 탭). 실패: None.
    성공 판정 = '참가' 탭 → 피크민 자동 선택 → 확정까지 완료(finish_join). 단계 스크린샷 joins/ 참조.
    """
    wid, b = win
    f = "/tmp/pikmin_join.png"
    open(SWEEP_LOCK, "w").close()          # 참가 조작 중 복구 데몬이 마우스를 못 건드리게
    try:
        subprocess.run(["osascript", "-e", 'tell application "iPhone Mirroring" to activate'], capture_output=True)
        time.sleep(0.4)
        # 1) 카드 찾기 (필요하면 리스트를 넘기며)
        target, swipes = None, 0
        while True:
            if not capture_window(wid, f):
                return None
            cards = giant_card_boxes(ocr_boxes(f))
            if prefer_keys:
                # 절단 길이가 달라 키가 정확히 안 맞을 수 있으니 접두사 매칭으로 찾는다
                target = next((c for c in cards
                               if any(same_card(c[0], k) for k in prefer_keys)), None)
            elif known_cards is not None:
                target = next((c for c in cards
                               if known_card_key(known_cards, c[0]) is None), None)
            if target is not None or swipes >= JOIN_MAX_SWIPES:
                break
            list_swipe(b, -380)
            swipes += 1
            time.sleep(1.0)
        if target is None:
            for _ in range(swipes + 2 if swipes else 0):    # 리스트 원위치
                list_swipe(b, +380)
                time.sleep(0.6)
            if map_xy is None:
                log("참가: 거대 카드를 찾지 못함")
                return None
            log(f"참가: 카드 못 찾음 → 맵 아이콘 직접 탭 ({map_xy[0]},{map_xy[1]})")
            tapped = True
            tap(b, map_xy[0], map_xy[1])
        else:
            key, disp, cx, cy = target
            tapped = key
            log(f"참가: 카드 탭 '{disp}' ({int(cx)},{int(cy)})")
            tap(b, cx, cy)
        time.sleep(1.2)          # 9/13: 2.5 → 1.2 (상세 화면은 보통 1초 안에 뜬다)
        # 2) 상세 화면에서 '참가' 버튼 찾기.
        # ⚠️ 탭이 씹히는 경우가 있다(9/19 17:39 사고): 카드가 화면 오른쪽에 있을 때 제목 텍스트의
        # OCR 중심을 탭했는데, 제목은 카드 왼쪽 정렬이라 그 좌표가 카드 경계 근처였고 카루셀이
        # 스냅 중이라 히트박스를 벗어났다. 그런데 예전 코드는 재탭을 안 하고 화면만 6초 보다가
        # 포기하고 헛알람을 울렸다. 그래서 '아직 리스트 화면'이면 좌표를 옮겨가며 다시 탭한다.
        # 오프셋은 제목 중심 기준: 카드 중앙 쪽(오른쪽), 카드 이미지 영역(위), 반대쪽 순.
        retaps = [(110, 0), (0, -75), (-110, 0)] if target is not None else []
        last_texts = []
        for attempt in range(6):
            if not capture_window(wid, f):
                return None
            join_snap(f, f"step{attempt}")
            bx = ocr_boxes(f)
            last_texts = [t.strip() for t, _, _ in bx]
            # 참가자 줄 첫 칸이 '당신'이면 이미 참가 중 — 참가 버튼이 없는 게 정상이니
            # 실패로 보지 말고 조용히 종료 (2026-09-13 사용자 리포트: 이미 참가 중인데 알림 옴)
            if "당신" in last_texts:
                if known_cards is not None and isinstance(tapped, str):
                    # 같은 버섯의 옛 변형 키가 있으면 그 항목을 갱신(새 키를 또 만들지 않는다)
                    known_cards[known_card_key(known_cards, tapped) or tapped] = time.time()
                log(f"참가: 이미 참가 중(카드에 '당신' 있음) → 알림 생략 snap={join_snap(f, 'already')}")
                return "already_joined"
            btn = next(((t.strip(), x, y) for t, x, y in bx
                        if t.strip() in JOIN_BUTTON_TEXTS), None)
            if btn is None:
                time.sleep(0.7)          # 9/13: 2.0 → 0.7 (재시도 횟수를 늘려 총 대기는 유지)
                continue
            log(f"참가: '{btn[0]}' 버튼 탭 ({int(btn[1])},{int(btn[2])})")
            tap(b, btn[1], btn[2])
            # '참가' 탭 후엔 '보낼 피크민 선택' 화면이 이어짐 — 자동 선택→확정까지 끝내야 진짜 참가
            fin = finish_join(wid, b)
            if fin == "needs_ticket":
                return "needs_ticket"      # 풀방이라 티켓 필요 → 알람 없이 넘어간다
            if fin:
                log("참가 성공(피크민 전송 완료)")
                return tapped
            return None
        p = join_snap(f, "nobtn")
        # 탭해서 열린 상세가 버섯이 아니면(레몬 등 과일 탐험 상세) 실패가 아니라 오탐 — 알람 대신 기각 (8/23 레몬 사건)
        if any(mk in " ".join(last_texts) for mk in NOT_GIANT_MARKERS):
            log(f"참가: 열린 상세가 버섯이 아님(과일/탐험 오브젝트) → 오탐 기각 snap={p}")
            return "not_giant"
        log(f"참가: '참가' 버튼을 찾지 못함 snap={p}")
        return None
    finally:
        try:
            os.remove(SWEEP_LOCK)
        except Exception:
            pass


def notify_joined(reason):
    """참가 성공 시 조용한 알림 한 번(반복 알람 대신) — 봇이 대신 참가했음을 알려만 준다"""
    log(f"✅ 자동 참가 완료 ({reason})")
    subprocess.run(["osascript", "-e",
                    'display notification "봇이 거대버섯 도전에 참가했습니다" with title "🍄 거대버섯 자동 참가"'],
                   capture_output=True)
    subprocess.run(["afplay", "-v", "2", "/System/Library/Sounds/Glass.aiff"], capture_output=True)


def handle_new_giant(win, state, reason, prefer_keys=None, map_xy=None, alert_on_fail=True):
    """새 거대 확정 시 처리(8/22): 직접 참가 시도 → 성공하면 배너 알림 1회, 실패하면 기존 반복 알람 폴백.
    참가한 카드는 known_cards에 등록해 같은 카드로 다시 울리지 않게 한다.

    alert_on_fail=False: '아는 카드 재확인' 경로에서 쓴다. 새 거대라는 확신이 없는 확인 작업이므로
    참가에 실패해도(이미 꽉 찼거나 참가 버튼이 없거나) 알람을 울리지 않는다. 안 그러면 재확인
    주기마다 헛알람이 난다."""
    if AUTO_JOIN:
        res = None
        try:
            res = join_giant(win, prefer_keys=prefer_keys, map_xy=map_xy,
                             known_cards=state.get("known_cards") or {})
        except Exception as e:
            log(f"참가 시도 오류: {e}")
        if res == "not_giant":
            # 버섯이 아닌 것(과일 등)을 탭한 오탐 → 그 자리를 기각 목록에 넣고 알람 없이 종료
            if map_xy is not None:
                state.setdefault("rejected", []).append(
                    {"x": map_xy[0], "y": map_xy[1], "ts": time.time()})
                # 감지 단계에서 sprites에 등록된 같은 자리 항목은 제거(기각 목록으로 일원화)
                state["sprites"] = [
                    s for s in state.get("sprites", [])
                    if not (abs(s["x"] - map_xy[0]) < KNOWN_RADIUS and abs(s["y"] - map_xy[1]) < KNOWN_RADIUS)
                ]
                save_state(state)
            log("오탐(버섯 아님) → 알림 생략")
            return
        if res == "needs_ticket":
            # 5명이 찬 방 — 티켓을 써야만 들어갈 수 있다. 티켓은 유한하므로 봇이 임의로 쓰지 않는다.
            # 같은 카드로 반복 시도하지 않도록 확인 시각만 기록하고 조용히 끝낸다.
            kc3 = state.setdefault("known_cards", {})
            for k3 in (prefer_keys or []):
                kc3[known_card_key(kc3, k3) or k3] = time.time()
            save_state(state)
            log("풀방(5명)이라 티켓이 필요함 → 티켓 안 쓰고 건너뜀, 알림 생략")
            return
        if res == "already_joined":
            # known_cards는 join_giant 안에서 이미 갱신됨(같은 dict 참조) — 저장만 하면 됨
            save_state(state)
            log("이미 참가 중인 거대 → 알림 생략")
            return
        if res:
            if isinstance(res, str):
                kc2 = state.setdefault("known_cards", {})
                kc2[known_card_key(kc2, res) or res] = time.time()
                save_state(state)
            notify_joined(reason)
            return
        if not alert_on_fail:
            log("재확인 결과 참가 못 함(이미 찼거나 참가 버튼 없음) → 알람 생략")
            return
        log("참가 실패 → 기존 알림으로 폴백")
    alert_until_confirm(reason)


_last_frame_save = 0.0


def keep_frame(tmp):
    """30초마다 현재 프레임을 frames/에 저장, 최근 240장만 유지 — '왜 못 잡았나' 사후 확인용"""
    global _last_frame_save
    now = time.time()
    if now - _last_frame_save < FRAME_EVERY:
        return
    _last_frame_save = now
    try:
        os.makedirs(FRAME_DIR, exist_ok=True)
        subprocess.run(["cp", tmp, os.path.join(FRAME_DIR, datetime.now().strftime("f_%Y%m%d_%H%M%S.png"))])
        fs = sorted(f for f in os.listdir(FRAME_DIR) if f.startswith("f_"))
        for f in fs[:-FRAME_KEEP]:
            os.remove(os.path.join(FRAME_DIR, f))
    except Exception:
        pass


def snap(tmp):
    p = os.path.join(SNAP_DIR, datetime.now().strftime("hit_%Y%m%d_%H%M%S.png"))
    subprocess.run(["cp", tmp, p])
    return p


def main():
    os.makedirs(SNAP_DIR, exist_ok=True)
    if "--reset" in sys.argv and os.path.exists(STATE_PATH):
        os.remove(STATE_PATH)
        log("state.json 초기화")

    templates = load_templates()
    if not templates:
        log("템플릿이 없음! templates/ 에 거대버섯 png 필요")
        sys.exit(1)
    log(f"감시 시작 v5(사이즈+LLM 판정). 템플릿 {len(templates)}개, 주기 {INTERVAL_SEC}s")

    # 맵 기준선(sprites)은 켤 때마다 리셋. known_cards(이미 참가한 카드)만 재시작을 넘어 유지
    # — 재시작 직후 이미 참가한 카드가 스크롤로 다시 보일 때 또 울리는 것 방지용.
    # 단, 화면에 보인다는 이유만으로 카드를 여기에 넣지는 않는다(9/13 수정, 기준선 블록 참조)
    old = load_state()
    old_cards = old.get("known_cards") or {}
    # 예전 형식(제목만, '|' 없음) 키는 버림 — 새 형식(제목|장소)과 호환 안 됨.
    # 또 9/13 이전 키는 장소명 정규화가 없어 '황제팽귄의.' 처럼 문장부호가 섞여 있다 → 다시 정규화
    migrated = {}
    for k, v in old_cards.items():
        if "|" not in k:
            continue
        head, loc = k.split("|", 1)
        migrated[f"{head}|{norm_loc(loc)}"] = max(v, migrated.get(f"{head}|{norm_loc(loc)}", 0))
    if migrated != {k: v for k, v in old_cards.items() if "|" in k}:
        log(f"known_cards 키 정규화 마이그레이션: {len(old_cards)}개 → {len(migrated)}개")
    state = {"sprites": [], "rejected": [], "known_cards": migrated,
             "quiet_until": old.get("quiet_until", 0), "armed_day": old.get("armed_day")}
    sprite_first = True
    candidates = []       # 맵 신규 아이콘 연속 관측 후보
    card_streak = 0       # 카드 수 증가 연속 관측
    last_invite_alert = 0.0
    last_llm = 0.0
    last_sweep = 0.0      # 리스트 넘겨보기 마지막 시각
    last_hash = None      # 화면 멈춤(미러링 끊김) 감지용
    prev_gate = None      # 알림 게이트 직전 상태 — 정지→재개 전환 감지용
    last_map_ts = None    # 마지막으로 맵 프레임을 처리한 시각 — 맵이 안 보이던 공백은 유예시간에서 제외
    same_count = 0
    tmp = "/tmp/pikmin_frame.png"

    while True:
        try:
            if not in_service_window():
                heartbeat("off_hours")       # 평일: 아무것도 안 봄
                candidates = []
                card_streak = 0
                time.sleep(20)
                continue
            win = find_mirror_window()
            if win is None:
                heartbeat("no_window")
                time.sleep(INTERVAL_SEC)
                continue
            if not capture_window(win[0], tmp):
                heartbeat("no_window")
                time.sleep(INTERVAL_SEC)
                continue

            # ---------- 화면 멈춤 감지: 완전 동일 프레임 연속 = 미러링 끊김 ----------
            with open(tmp, "rb") as f:
                h = hashlib.md5(f.read()).hexdigest()
            if h == last_hash:
                same_count += 1
            else:
                same_count = 0
            last_hash = h
            if same_count >= FROZEN_FRAMES:
                heartbeat("no_window")
                candidates = []
                card_streak = 0
                time.sleep(INTERVAL_SEC)
                continue

            boxes = ocr_boxes(tmp)
            texts = [t for t, _, _ in boxes]
            joined = " ".join(texts)

            # ---------- 끊김 안내 오버레이 감지 ----------
            if any(w in joined for w in DISCONNECT_WORDS):
                heartbeat("no_window")
                candidates = []
                card_streak = 0
                time.sleep(INTERVAL_SEC)
                continue

            # ---------- 하루 도전 횟수 게이트 ----------
            if update_quota(state, texts):
                save_state(state)
            gate = alert_state(state)
            if prev_gate is not None and prev_gate != "ok" and gate == "ok":
                # 정지(횟수 소진/확인 대기) → 재개: 지금 보이는 거대는 정지 중에 뜬 것 → 기준선으로 등록만 하고
                # 알리지 않는다. 재개 이후 새로 뜨는 것만 새 거대 (사용자 요청 8/15)
                log("알림 재개 → 지금 화면의 거대는 기준선으로 등록(정지 중 등장분 제외)")
                sprite_first = True
                candidates = []
                card_streak = 0
            prev_gate = gate
            if gate != "ok":
                heartbeat(gate)               # quiet: 오늘 소진 / not_armed: 오늘 횟수 확인 전
                candidates = []
                card_streak = 0
                time.sleep(INTERVAL_SEC)
                continue

            # ---------- C) 초대 푸시 배너 감지 (화면 종류 무관, 최우선) ----------
            # 오탐 방지 2중 조건: ① '초대'가 있어도 '초대하기' 같은 버튼 라벨 줄은 무시
            # ② 화면 어딘가에 피크민 맥락 단어(버섯/도전 등)가 같이 보여야 푸시로 인정
            invite_hit = next(
                (t for t in texts
                 if any(w in t for w in INVITE_WORDS)
                 and not any(x in t for x in INVITE_EXCLUDE)),
                None,
            )
            if invite_hit and any(c in joined for c in INVITE_CONTEXT):
                now = time.time()
                if now - last_invite_alert >= INVITE_COOLDOWN:
                    last_invite_alert = now
                    p = snap(tmp)
                    log(f"📩 초대 배너 감지!! '{invite_hit}' snap={p}")
                    alert_until_confirm("초대 푸시")

            # 탭바 마커가 2개 이상 보여야 맵(탐험 탭). 홈 화면의 "N건의 탐험이 있습니다" 배너 하나로는 오판(8/15)
            if sum(1 for m in MAP_MARKERS if m in joined) < 2:
                heartbeat("not_map")
                candidates = []
                card_streak = 0
                time.sleep(INTERVAL_SEC)
                continue

            heartbeat("watching")
            keep_frame(tmp)
            # 맵이 안 보이던 공백(폰 사용/홈 화면/끊김) 동안은 아는 것/기각 목록이 늙지 않게 시각을 밀어준다
            # (22:31 오알림 원인: 7분 홈 화면 뒤 복귀하니 유예 300초가 지나 구룡의 장식 피크민을 새 거대로 재판정)
            _now = time.time()
            if last_map_ts is not None and _now - last_map_ts > INTERVAL_SEC * 3:
                _gap = _now - last_map_ts
                for _s in state.get("sprites", []) + state.get("rejected", []):
                    if "ts" in _s:
                        _s["ts"] += _gap
                log(f"맵 복귀: 공백 {int(_gap)}초 동안 아는 목록 유지")
            last_map_ts = _now
            tpl_hits = find_giants(tmp, templates)
            size_cands = find_size_candidates(tmp)
            # 템플릿 히트(생김새 확정) + 사이즈 후보(생김새 무관) 합침. 겹치면 템플릿 우선
            giants = tpl_hits + [
                c for c in size_cands
                if not any(dist_ok(c, g, CANDIDATE_RADIUS) for g in tpl_hits)
            ]
            # 카드 제목 수집: '거대'로 시작하는 카드 타이틀만 (잘린 제목 대비 앞 12자를 키로)
            seen_cards = card_keys(boxes)

            # ---------- 첫 프레임: 기준선 등록 ----------
            if sprite_first:
                for g in giants:
                    log(f"기준선(맵): ({g['x']},{g['y']}) [{g['tpl']}]")
                    state["sprites"].append({"x": g["x"], "y": g["y"], "ts": time.time(), "tpl": g["tpl"]})
                # 카드는 기준선으로 등록하지 않는다(9/13 수정). 예전엔 켜는 순간 화면에 보이던
                # 거대 카드를 전부 '아는 것'으로 등록했는데, known_cards는 재시작을 넘어 유지되므로
                # 아직 아무도 안 들어간 멀쩡한 거대가 재시작 한 번으로 영구히 무시됐다
                # (9/13 새벽 '황제팽귄의 얼음낚시놀이터' 3시간 방치 사고).
                # 이미 참가한 카드는 탭했을 때 참가자 줄의 '당신'으로 판별해 조용히 넘긴다.
                if seen_cards:
                    log(f"시작 시 보이는 거대 카드(참가 대상으로 취급): {list(seen_cards.values())}")
                save_state(state)
                zero_in_map(win[1])
                sprite_first = False
                time.sleep(INTERVAL_SEC)
                continue

            # ---------- A) 맵 아이콘 감지 ----------
            rejected = state.setdefault("rejected", [])
            new_candidates = []
            for g in giants:
                k = next((s for s in state["sprites"] if dist_ok(g, s, KNOWN_RADIUS)), None)
                if k is not None and g["tpl"] != "size" and k.get("tpl") == "size":
                    # 확실한 템플릿 히트가 '미확인 사이즈 후보(군집 등)' 근처에 뜬 것 → 새 거대.
                    # 기준선의 size 항목은 거대가 아닌 경우가 많아 그 옆의 진짜 거대를 삼키면 안 됨
                    k = None
                if k is not None:
                    if not dist_ok(g, k, 10):
                        k.update(x=g["x"], y=g["y"])
                        save_state(state)
                    continue
                rj = next((r for r in rejected if dist_ok(g, r, REJECT_RADIUS)), None)
                if rj is not None:                       # LLM이 군집 등으로 기각한 자리 → 무시
                    if not dist_ok(g, rj, 10):
                        rj.update(x=g["x"], y=g["y"])
                        save_state(state)
                    continue
                prev = next((c for c in candidates if dist_ok(g, c, CANDIDATE_RADIUS)), None)
                hits = (prev["hits"] + 1) if prev else 1
                if hits >= CONFIRM_FRAMES:
                    if g["tpl"] == "size":
                        # 생김새를 모르는 후보 → LLM 최종 판정 (쿨다운 내면 다음 루프에 재시도)
                        if time.time() - last_llm < LLM_COOLDOWN:
                            new_candidates.append({"x": g["x"], "y": g["y"], "hits": hits})
                            continue
                        last_llm = time.time()
                        res = llm_is_giant(tmp, g["x"], g["y"])
                        if res is None:                  # 판정 실패 → 후보 유지, 재시도
                            new_candidates.append({"x": g["x"], "y": g["y"], "hits": hits})
                            continue
                        verdict, label = res
                        if not verdict:
                            log(f"LLM 기각(군집/기타): ({g['x']},{g['y']})")
                            rejected.append({"x": g["x"], "y": g["y"]})
                            save_state(state)
                            # 군집이라 애매하면 리스트를 끝까지 넘겨 거대 카드가 새로 있는지 확인
                            if any(label.startswith(k) for k in SWEEP_ON) and time.time() - last_sweep >= SWEEP_COOLDOWN:
                                last_sweep = time.time()
                                found = sweep_cards(win)
                                kc = state.setdefault("known_cards", {})
                                new_keys = [k2 for k2 in found
                                            if known_card_key(kc, k2) is None]
                                for k2 in found:
                                    kc[known_card_key(kc, k2) or k2] = time.time()
                                save_state(state)
                                if new_keys:
                                    p = snap(tmp)
                                    log(f"🍄 새 거대 카드(리스트 넘겨보기)!! {[found[k2] for k2 in new_keys]} snap={p}")
                                    handle_new_giant(win, state, "리스트 카드(군집 확인)", prefer_keys=set(new_keys))
                            continue
                    p = snap(tmp)
                    # 새로 확정된 거대는 근처의 미확인 size 기준선을 대체(같은 자리 이중 추적 방지)
                    state["sprites"] = [
                        s for s in state["sprites"]
                        if not (s.get("tpl") == "size" and dist_ok(g, s, KNOWN_RADIUS))
                    ]
                    state["sprites"].append({"x": g["x"], "y": g["y"], "ts": time.time(),
                                             "tpl": g["tpl"] if g["tpl"] != "size" else "llm"})
                    save_state(state)
                    log(f"🍄 맵에 새 거대!! ({g['x']},{g['y']}) [{g['tpl']}] snap={p}")
                    handle_new_giant(win, state, "맵 아이콘", map_xy=(g["x"], g["y"]))
                else:
                    new_candidates.append({"x": g["x"], "y": g["y"], "hits": hits})
            candidates = new_candidates
            # 이번 프레임에 보인 항목은 최근 관측 시각 갱신, 유예시간 넘게 안 보인 것만 제거
            # (전투 애니메이션으로 한두 프레임 끊겨도 바로 지우면 재등장 시 재알람 루프가 생김)
            now = time.time()
            for s in state["sprites"]:
                if any(dist_ok(g, s, KNOWN_RADIUS) for g in giants):
                    s["ts"] = now
            state["sprites"] = [
                s for s in state["sprites"] if now - s.get("ts", now) < SPRITE_GRACE_SEC
            ]
            for r in state["rejected"]:
                if any(dist_ok(g, r, REJECT_RADIUS) for g in giants):
                    r["ts"] = now
            state["rejected"] = [
                r for r in state["rejected"] if now - r.get("ts", now) < REJECT_GRACE_SEC
            ]

            # ---------- B) 리스트 카드 감지 (원거리/초대 포함) ----------
            # 개수 대신 카드 '제목'을 기억한다. 가로 리스트 스크롤로 아는 카드가
            # 보였다 안 보였다 해도(8/2 오탐 원인) 처음 보는 제목일 때만 알린다.
            known_cards = state.setdefault("known_cards", {})
            # ⚠️ 보일 때마다 타임스탬프를 갱신하지 않는다(9/13 수정). 예전엔 카드가 화면에 보이는 동안
            # 계속 갱신해서 만료가 영원히 안 왔고, 같은 자리 리스폰을 전부 놓쳤다.
            # 타임스탬프는 '마지막으로 탭해서 확인한 시각'만 기록한다.
            state["known_cards"] = {
                k2: v for k2, v in known_cards.items() if now - v < KNOWN_CARD_TTL
            }
            known_cards = state["known_cards"]
            # 리스트 위치로 참가 여부를 판정한다. 참가한 카드는 첫 화면에 오지 않으므로
            # '버섯 개수: 1/M' 화면에 거대 카드가 보이면 안 들어간 것이다 → 같은 장소 키가
            # known_cards에 있어도(어제 참가했다가 사라지고 같은 자리에 다시 뜬 경우) 참가 대상으로 본다.
            page = parse_card_page(texts)
            first_page = page is not None and page[0] == 1
            fresh_keys, recheck_keys = [], []
            for k2 in seen_cards:
                hit = known_card_key(known_cards, k2)
                if hit is None:
                    fresh_keys.append(k2)                                   # 처음 보는 카드
                elif not AUTO_JOIN:
                    continue                     # 탭해서 확인할 수단이 없으면 재확인 의미 없음
                elif first_page and now - known_cards[hit] >= FIRSTPAGE_RETRY_SEC:
                    recheck_keys.append(k2)      # 첫 화면 = 미참가 확정(리스폰이거나 자리가 났음)
                elif now - known_cards[hit] >= KNOWN_CARD_RECHECK_SEC:
                    recheck_keys.append(k2)      # 위치 신호를 못 쓴 경우의 보조 안전망
            if fresh_keys:
                # 카드는 OCR로 '거대' 글자를 직접 읽은 것이라 확신도가 높다 → 연속 관측 기다리지 않고
                # 즉시 처리한다(9/13 수정: 예전엔 CONFIRM_FRAMES=2 때문에 6초를 그냥 버렸다.
                # 낮에는 그 6초 안에 자리가 찬다)
                p = snap(tmp)
                for k2 in fresh_keys:
                    known_cards[k2] = now
                save_state(state)
                card_streak = 0
                log(f"🍄 새 거대 카드!! {[seen_cards[k2] for k2 in fresh_keys]} snap={p}")
                handle_new_giant(win, state, "리스트 카드", prefer_keys=set(fresh_keys))
            elif recheck_keys:
                # 아는 카드지만 확인이 오래됐다 → 탭해서 '당신'이 있는지 본다.
                # 있으면 join_giant가 already_joined로 조용히 끝내고, 없으면 새 출현이라 참가한다.
                # 실패해도 새 거대라는 확신이 없으므로 알람은 울리지 않는다.
                for k2 in recheck_keys:
                    known_cards[known_card_key(known_cards, k2) or k2] = now
                save_state(state)
                card_streak = 0
                why = (f"첫 화면({page[0]}/{page[1]})에 보임 → 미참가 확정"
                       if first_page else f"{KNOWN_CARD_RECHECK_SEC}초 경과(위치 신호 없음)")
                log(f"재확인: 아는 거대 카드 {[seen_cards[k2] for k2 in recheck_keys]} — {why}")
                handle_new_giant(win, state, "아는 카드 재확인", prefer_keys=set(recheck_keys),
                                 alert_on_fail=False)
            else:
                card_streak = 0

            # ---------- D) 정기 전체 목록 점검 ----------
            # 위 B)는 '지금 화면에 보이는' 카드만 본다. 카루셀은 12장 중 1~2장만 노출되므로
            # 뒤쪽에 뜬 거대는 존재조차 모른다. 예전엔 전체 점검이 LLM CLUSTER 판정에 딸린
            # 조건부라 58분간 안 돈 적이 있다(9/19). 이제 주기적으로 무조건 한 번 훑는다.
            if time.time() - last_sweep >= SWEEP_INTERVAL_SEC:
                last_sweep = time.time()
                found = sweep_cards(win)
                kc = state.setdefault("known_cards", {})
                sweep_new = [k2 for k2 in found if known_card_key(kc, k2) is None]
                for k2 in found:
                    kc[known_card_key(kc, k2) or k2] = time.time()
                save_state(state)
                if sweep_new:
                    p = snap(tmp)
                    log(f"🍄 새 거대 카드(정기 점검)!! {[found[k2] for k2 in sweep_new]} snap={p}")
                    handle_new_giant(win, state, "리스트 정기 점검", prefer_keys=set(sweep_new))
                elif found:
                    log(f"정기 점검: 거대 카드 {list(found.values())} (전부 확인된 것)")

        except KeyboardInterrupt:
            log("종료")
            sys.exit(0)
        except Exception as e:
            log(f"오류: {e}")
        time.sleep(INTERVAL_SEC)


if __name__ == "__main__":
    main()

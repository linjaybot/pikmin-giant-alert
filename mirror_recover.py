#!/usr/bin/env python3
"""
아이폰 미러링 + 피크민 블룸 자동 복구 데몬 (start.sh로 실행, launchd 아님)

launchd로 돌리면 화면 캡처/접근성 권한이 없어 장님 상태가 되므로,
감시봇(pikmin_watch.py)과 같은 터미널 컨텍스트에서 nohup으로 띄운다.

루프(기본 40초):
  1) 미러링 앱 꺼짐            → 실행
  2) 창 없음                   → 앱 활성화
  3) OCR "사용 중"             → 폰 사용 중, 대기 (애플 제한)
  4) OCR 맵 마커(탐험/모종/엽서) → 정상, 아무것도 안 함
  5) 그 외(일시정지/끊김/홈화면/팝업/피크민 다른 화면)
       → 2회 연속이면 시각 판단(claude -p)으로 클릭/앱 실행/대기 결정, 최대 8스텝
로그: ~/pikmin-watch/recover.log
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.expanduser("~/pikmin-watch"))
from pikmin_watch import (  # noqa: E402
    find_mirror_window, capture_window, ocr_texts, MAP_MARKERS, CLAUDE_CLI, in_service_window, SWEEP_LOCK,
)

BASE = os.path.expanduser("~/pikmin-watch")
LOG_PATH = os.path.join(BASE, "recover.log")
FRAME = "/tmp/pikmin_recover.png"
APP = "iPhone Mirroring"
INTERVAL = 20              # 8/15: 40→20 (비정상 확인까지 최대 80초 걸리던 것을 절반으로)
UNHEALTHY_STREAK = 1       # 8/15: 2→1. 미러링이 살아있는데 맵이 아니면 곧바로 복구(사람이 폰을 쓰면 어차피 paused)
MAX_STEPS = 8              # 한 번의 복구 세션에서 시각 판단 최대 스텝
SAME_CLICK_LIMIT = 2       # 같은 자리 클릭 판단이 이 횟수 연속이면 세션 중단(효과 없는 클릭 반복 금지)
FAIL_BACKOFF = 300         # 복구 실패 3회 연속이면 이만큼(초) 쉬고 재시도
KEEPAWAKE_EVERY = 300      # 맥 화면보호기/잠금 방지: 이 간격(초)으로 마우스 1px 이동+복귀 (8/16 새벽 3시간 무입력 → 잠금 → 미러링 9시간 정지 재발 방지)
LLM_TIMEOUT = 120
# ⚠️ 9/20 사고: "iPhone 사용 중 — 연결하려면 iPhone을 잠그십시오" 상태가 7시간 지속됐는데
# 사용자에게 아무 알림이 없었다. 이 상태는 사람이 폰을 잠가야만 풀리므로(애플 제한) 반드시 알려야 한다.
PAUSED_ALERT_AFTER = 300      # paused가 이만큼(초) 지속되면 첫 알림
PAUSED_ALERT_REPEAT = 1800    # 이후 반복 알림 간격(초)
BUSY_WORDS = ("사용 중", "종료되었습니다", "잠그십시오", "시간 초과", "연결하려면", "연결하기 전에", "다시 시도", "오류가 발생",
              "일시 정지", "일시정지", "연결이 끊", "연결할 수 없")  # 8/16: "연결이 일시 정지됨 [재개]"를 9시간 동안 LLM 클릭 루프로 돌린 사고 방지  # 미러링 안내/오류 화면(폰 잠그면 [연결]/[다시 시도] 눌러야 붙음)
CLICK_COOLDOWN = 20        # 클릭 후 화면 반영 대기
EXPLORE_BANNER = "탐험이 있습니다"   # 홈 화면 상단 배너 — 보이면 LLM 없이 바로 탭
APP_LABEL = "Pikmin Bloom"        # 아이폰 홈/스포트라이트의 앱 아이콘 라벨 — 보이면 아이콘(라벨 위 55px) 탭
APP_ICON_DY = 55                  # 라벨 중심 → 아이콘 중심 세로 거리(px, 644폭 기준)
LAUNCH_WAIT = 45           # 앱 실행 후 배너/맵이 뜰 때까지 폴링 상한(초)
_auto_wait_logged = False


def log(msg):
    line = f"[{datetime.now().strftime('%m/%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def app_running():
    return subprocess.run(["pgrep", "-xq", APP]).returncode == 0


def activate():
    subprocess.run(["osascript", "-e", f'tell application "{APP}" to activate'],
                   capture_output=True)


def tap_app_icon(b, frame):
    """화면에 'Pikmin Bloom' 라벨이 보이면 그 위 아이콘을 탭. 키보드 포커스와 무관하게 동작"""
    for t, cx, cy in ocr_boxes(frame):
        if APP_LABEL in t:
            ok, x, y = click_image_point(b, frame, cx, cy - APP_ICON_DY)
            log(f"'{APP_LABEL}' 아이콘 탭 ({x},{y}) ok={ok}")
            return ok
    return False


def launch_pikmin(b=None):
    """앱 실행. ① 홈 화면에 아이콘 보이면 탭 ② ⌘3 스포트라이트 → Siri 제안의 아이콘 탭 ③ 그래도 없으면 'pikmin' 타이핑.
    (22:25 실패 원인: 타이핑은 맥의 키보드 포커스가 다른 앱(크롬 등)에 있으면 아무 데도 안 들어감 → 클릭 우선)"""
    if b is not None:
        win = find_mirror_window()
        if win and capture_window(win[0], FRAME) and tap_app_icon(win[1], FRAME):
            return True
        activate()
        time.sleep(1.0)
        subprocess.run(["osascript", "-e",
                        f'tell application "System Events" to tell process "{APP}" to keystroke "3" using command down'],
                       capture_output=True)
        time.sleep(2.5)
        win = find_mirror_window()
        if win and capture_window(win[0], FRAME) and tap_app_icon(win[1], FRAME):
            return True
        log("스포트라이트에 아이콘 안 보임 → 타이핑 시도")
    script = f'''
tell application "{APP}" to activate
delay 2
tell application "System Events"
  tell process "{APP}"
    keystroke "3" using command down
    delay 1.5
    keystroke "pikmin"
    delay 2
    key code 36
  end tell
end tell
'''
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if r.returncode != 0:
        log(f"피크민 실행 키입력 실패: {r.stderr.strip()[:160]}")
        return False
    log("피크민 실행 키입력 전송(⌘3 → pikmin → ↩)")
    return True


def click_screen(x, y):
    activate()
    time.sleep(0.8)
    r = subprocess.run(["/opt/homebrew/bin/cliclick", f"c:{x},{y}"], capture_output=True, text=True)
    if r.returncode != 0:
        log(f"클릭 실패 ({x},{y}): {r.stderr.strip()[:120]}")
        return False
    return True


def image_size(path):
    from PIL import Image
    with Image.open(path) as im:
        return im.size


def llm_decide(frame):
    prompt = (
        f"Read {frame} — 맥의 아이폰 미러링 창 스크린샷이다(이미지 크기는 창의 2배 해상도일 수 있음). "
        "목표: 폰에서 '피크민 블룸' 앱의 하단 패널 '탐험' 탭(버섯 목록, '탐험이 있습니다')이 보이는 상태로 만들기.\n"
        "화면을 보고 다음 중 하나의 JSON 한 줄만 출력하라. 다른 텍스트 금지:\n"
        '{"action":"done"} — 이미 피크민 블룸의 맵+하단 패널(탐험/모종/엽서 탭)이 보임. 이 경우 절대 click을 고르지 말 것(맵 위 버섯·아이콘 탭 금지)\n'
        '{"action":"busy"} — "iPhone 사용 중 / 연결하려면 iPhone을 잠그십시오" 안내 화면\n'
        '{"action":"click","px":이미지픽셀x,"py":이미지픽셀y} — 다음에 탭/클릭할 위치. '
        "미러링 일시정지/끊김 화면이면 그 재개·다시 연결 버튼(없으면 창 중앙), "
        "피크민 안이면 '탐험이 있습니다' 배너나 하단 '탐험' 탭, 팝업/공지가 가리면 그 닫기(X) 버튼\n"
        '{"action":"launch"} — 피크민이 아닌 다른 앱/iOS 홈 화면/잠금 화면이라 피크민을 실행해야 함\n'
        '{"action":"wait"} — 로딩 중이라 기다려야 함'
    )
    try:
        proc = subprocess.run(
            [CLAUDE_CLI, "-p", prompt, "--model", "sonnet", "--allowedTools", "Read"],
            capture_output=True, text=True, timeout=LLM_TIMEOUT, cwd=BASE,
        )
        lines = [l.strip() for l in proc.stdout.splitlines() if l.strip()]
        if proc.returncode != 0 or not lines:
            log(f"LLM 실패 rc={proc.returncode} err={proc.stderr[:160]}")
            return None
        last = lines[-1]
        start = last.find("{")
        return json.loads(last[start:]) if start >= 0 else None
    except Exception as e:
        log(f"LLM 오류: {e}")
        return None


def ocr_boxes(path):
    """[(text, cx_px, cy_px)] — 이미지 픽셀 좌표(좌상단 원점)"""
    import Quartz
    import Vision
    url = Quartz.CFURLCreateWithFileSystemPath(None, path, Quartz.kCFURLPOSIXPathStyle, False)
    src = Quartz.CGImageSourceCreateWithURL(url, None)
    if src is None:
        return []
    img = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
    if img is None:
        return []
    W, H = image_size(path)
    out = []

    def handler(req, err):
        for o in (req.results() or []):
            c = o.topCandidates_(1)
            if c and len(c):
                bb = o.boundingBox()
                cx = (bb.origin.x + bb.size.width / 2) * W
                cy = (1 - (bb.origin.y + bb.size.height / 2)) * H
                out.append((str(c[0].string()), cx, cy))

    r = Vision.VNRecognizeTextRequest.alloc().initWithCompletionHandler_(handler)
    r.setRecognitionLanguages_(["ko-KR", "en-US"])
    r.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(img, None).performRequests_error_([r], None)
    return out


def click_image_point(b, frame, px, py):
    """이미지 픽셀 좌표 → 화면 좌표 변환 후 클릭"""
    iw, ih = image_size(frame)
    scale = iw / b["Width"]
    x = int(b["X"] + px / scale)
    y = int(b["Y"] + py / scale)
    ok = click_screen(x, y)
    return ok, x, y


def press_connect(b, frame):
    """'iPhone 사용 중 … 연결하려면 iPhone을 잠그십시오 [연결]' 화면의 연결 버튼 클릭.
    폰이 아직 사용 중이면 눌러도 그대로라 무해 → 매 주기 눌러서 잠기는 즉시 붙게 한다."""
    boxes = ocr_boxes(frame)
    for t, cx, cy in boxes:
        if t.strip() in ("연결", "다시 연결", "다시 시도", "재개", "계속"):
            ok, x, y = click_image_point(b, frame, cx, cy)
            log(f"'{t.strip()}' 버튼 클릭 ({x},{y}) ok={ok}")
            return ok
    joined = " ".join(t for t, _, _ in boxes)
    if "연결됩니다" in joined or "더 이상 사용되지" in joined:
        # [연결]을 이미 누른 뒤 상태: "iPhone이 더 이상 사용되지 않을 때 자동 연결" → 폰 잠그면 알아서 붙음, 대기
        global _auto_wait_logged
        if not _auto_wait_logged:
            log("자동 재연결 대기 중(폰 놓으면 붙음)")
            _auto_wait_logged = True
        return False
    log("연결 버튼 텍스트 못 찾음 → 시각 판단으로")
    d = llm_decide(frame)
    if d and d.get("action") == "click":
        ok, x, y = click_image_point(b, frame, d["px"], d["py"])
        log(f"LLM 클릭 ({x},{y}) ok={ok}")
        return ok
    return False


def find_text_box(frame, needle):
    """OCR 박스 중 needle을 포함하는 첫 줄의 (cx, cy) — 없으면 None"""
    for t, cx, cy in ocr_boxes(frame):
        if needle in t:
            return cx, cy
    return None


def try_fast_path(wid, b):
    """LLM 없이 처리 가능한 화면: ① 이미 탐험 탭 → True  ② '탐험이 있습니다' 배너 → 탭 후 True
    아무것도 아니면 None (LLM 판단 필요)"""
    if not capture_window(wid, FRAME):
        return None
    texts = ocr_texts(FRAME)
    if classify(texts) == "healthy":
        # 앱 실행 직후엔 이전 화면이 잠깐 보였다가 홈으로 리셋되기도 함(22:40) → 3초 뒤 한 번 더 확인
        time.sleep(3)
        if capture_window(wid, FRAME) and classify(ocr_texts(FRAME)) == "healthy":
            return True
        return None
    if any(EXPLORE_BANNER in t for t in texts):
        pos = find_text_box(FRAME, EXPLORE_BANNER)
        if pos:
            ok, x, y = click_image_point(b, FRAME, pos[0], pos[1])
            log(f"'{EXPLORE_BANNER}' 배너 탭 ({x},{y}) ok={ok}")
            time.sleep(3)          # 시트 열리는 애니메이션 동안 재탭 방지
            return True
    if any(APP_LABEL in t for t in texts):        # 홈/스포트라이트에 앱 아이콘 → 바로 탭
        if tap_app_icon(b, FRAME):
            time.sleep(4)
            return True
    return None


def wait_for(wid, b, seconds, step=3):
    """seconds 동안 step 간격으로 빠른 경로 시도. 탐험 화면 도달/배너 탭하면 True"""
    t0 = time.time()
    while time.time() - t0 < seconds:
        r = try_fast_path(wid, b)
        if r:
            return True
        time.sleep(step)
    return False


HB_PATH = os.path.join(BASE, "heartbeat.json")
REFRESH_EVERY = 180        # 자정 후 '횟수 확인 대기' 상태면 이 간격으로 탐험 시트 새로고침(탭 전환)


def watch_status():
    try:
        with open(HB_PATH) as f:
            return json.load(f).get("status")
    except Exception:
        return None


def refresh_explore_sheet(b, frame):
    """탐험 시트의 '오늘은 앞으로 N회'가 자정 넘어도 안 바뀔 때: 모종 탭 → 탐험 탭 순서로 눌러 다시 그리게 함"""
    boxes = ocr_boxes(frame)
    def find(label):
        return next(((cx, cy) for t, cx, cy in boxes if t.strip() == label), None)
    a, c = find("모종"), find("탐험")
    if not a or not c:
        log("탭 새로고침: 탭 글자 못 찾음")
        return False
    click_image_point(b, frame, a[0], a[1])
    time.sleep(2.5)
    click_image_point(b, frame, c[0], c[1])
    log("탐험 시트 새로고침(모종→탐험 탭 전환) — 자정 후 횟수 갱신 유도")
    return True


def keep_mac_awake():
    """맥이 3시간 무입력이면 화면보호기+잠금 → 미러링 '연결이 일시 정지됨'이 되고 봇이 재개할 수 없음.
    운영 시간엔 5분마다 마우스를 1px 움직였다 되돌려 유휴 타이머를 리셋한다(실사용에 영향 없음)."""
    try:
        r = subprocess.run(["/opt/homebrew/bin/cliclick", "p"], capture_output=True, text=True, timeout=5)
        x, y = [int(v) for v in r.stdout.strip().split(",")]
        subprocess.run(["/opt/homebrew/bin/cliclick", f"m:{x+1},{y}", f"m:{x},{y}"], capture_output=True, timeout=5)
        subprocess.run(["caffeinate", "-u", "-t", "2"], capture_output=True, timeout=10)
    except Exception as e:
        log(f"깨우기 실패: {e}")


def notify_paused(minutes):
    """폰을 잠가야만 풀리는 상태 — 봇이 스스로 할 수 있는 게 없으니 사람을 부른다."""
    log(f"⚠️ {minutes}분째 미러링 끊김(iPhone 사용 중) → 사용자 알림 발송")
    msg = f"아이폰을 잠가야 다시 연결됩니다. {minutes}분째 거대버섯 감시가 멈춰 있습니다."
    subprocess.run(["osascript", "-e",
                    f'display notification "{msg}" with title "🍄 피크민 감시 중단" sound name "Sosumi"'],
                   capture_output=True)
    subprocess.run(["afplay", "-v", "2", "/System/Library/Sounds/Sosumi.aiff"], capture_output=True)


def classify(texts):
    joined = " ".join(texts)
    if any(w in joined for w in BUSY_WORDS):
        return "paused"
    if sum(1 for m in MAP_MARKERS if m in joined) >= 2:
        return "healthy"
    return "unhealthy"


def recover(win):
    """시각 판단 루프. True=탐험 화면 도달, False=실패/대기"""
    log("복구 시작")
    last_click, same_click = None, 0
    for step in range(1, MAX_STEPS + 1):
        win = find_mirror_window()
        if win is None:
            log("복구 중 창 사라짐")
            return False
        wid, b = win
        if not capture_window(wid, FRAME):
            log("복구 중 캡처 실패")
            return False
        # OCR 빠른 판정: 이미 탐험 화면이면 LLM 안 거치고 종료 (LLM이 맵을 괜히 탭하는 오판 방지)
        texts = ocr_texts(FRAME)
        st = classify(texts)
        if st == "healthy":
            log("복구 완료: 탐험 화면 도달(OCR)")
            return True
        # 홈 화면 배너 '탐험이 있습니다'가 보이면 LLM 없이 바로 탭 (8/15: 스텝당 LLM 8~10초 절약)
        if any(EXPLORE_BANNER in t for t in texts):
            pos = find_text_box(FRAME, EXPLORE_BANNER)
            if pos:
                ok, x, y = click_image_point(b, FRAME, pos[0], pos[1])
                log(f"'{EXPLORE_BANNER}' 배너 탭 ({x},{y}) ok={ok}")
                time.sleep(3)      # 시트 열리는 동안 재탭 방지 (22:09 이중 탭 원인)
                if wait_for(wid, b, 15, step=2):
                    log("복구 완료: 탐험 화면 도달(배너 탭)")
                    return True
                continue
        if st == "paused":
            # 복구 중 폰을 다시 집었거나 미러링 오류 → 버튼만 누르고 복구 종료(메인 루프가 40초마다 이어감)
            press_connect(b, FRAME)
            return False
        if any(APP_LABEL in t for t in texts) and tap_app_icon(b, FRAME):
            if wait_for(wid, b, LAUNCH_WAIT, step=3) and wait_for(wid, b, 15, step=2):
                log("복구 완료: 탐험 화면 도달(아이콘 탭)")
                return True
            continue
        d = llm_decide(FRAME)
        log(f"step{step}: {d}")
        if not d:
            time.sleep(6)
            continue
        a = d.get("action")
        if a == "done":
            log("복구 완료: 탐험 화면 도달")
            return True
        if a == "busy":
            press_connect(b, FRAME)
            return False
        if a == "wait":
            time.sleep(10)
            continue
        if a == "launch":
            launch_pikmin(b)
            # 고정 35초 대기 대신 3초마다 화면 확인 → 배너 뜨는 즉시 탭 (8/15)
            if wait_for(wid, b, LAUNCH_WAIT, step=3) and wait_for(wid, b, 15, step=2):
                log("복구 완료: 탐험 화면 도달(실행 후 배너 탭)")
                return True
            continue
        if a == "click":
            key = (round(d["px"] / 20), round(d["py"] / 20))
            same_click = same_click + 1 if key == last_click else 1
            last_click = key
            if same_click > SAME_CLICK_LIMIT:
                log(f"같은 자리 클릭 판단 {same_click}회 연속 → 효과 없음, 세션 중단")
                return False
            # 판단(8~10초) 사이에 화면이 바뀌었을 수 있음 → 클릭 직전 재확인 (12:32 모종 오탭 원인)
            if not capture_window(wid, FRAME):
                return False
            st2 = classify(ocr_texts(FRAME))
            if st2 == "healthy":
                log("클릭 직전 재확인: 이미 탐험 화면 → 클릭 취소")
                return True
            if st2 == "paused":
                log("클릭 직전 재확인: 미러링 일시정지 화면 → 클릭 취소")
                press_connect(b, FRAME)
                return False
            iw, ih = image_size(FRAME)
            scale = iw / b["Width"]
            x = int(b["X"] + d["px"] / scale)
            y = int(b["Y"] + d["py"] / scale)
            click_screen(x, y)
            log(f"클릭 ({x},{y})")
            if wait_for(wid, b, CLICK_COOLDOWN if step == 1 else 8, step=2):
                log("복구 완료: 탐험 화면 도달(클릭 후)")
                return True
            continue
        time.sleep(6)
    log("복구 스텝 초과")
    return False


def main():
    log(f"미러링 복구 데몬 시작 (주기 {INTERVAL}s)")
    streak = 0
    last_state = None
    paused_since = None      # 'iPhone 사용 중'이 시작된 시각
    last_paused_alert = 0.0  # 마지막 알림 시각
    off_logged = False
    last_refresh = 0.0
    fail_streak = 0
    last_awake = 0.0
    while True:
        try:
            if not in_service_window():          # 평일: 폰/미러링 건드리지 않음
                if not off_logged:
                    log("평일 휴무 — 복구 동작 안 함 (토 00:00 재개)")
                    off_logged = True
                time.sleep(60)
                continue
            off_logged = False
            if os.path.exists(SWEEP_LOCK):           # 감시봇이 리스트를 넘기는 중 → 마우스/클릭 건드리지 않음
                time.sleep(5)
                continue
            if time.time() - last_awake > KEEPAWAKE_EVERY:
                last_awake = time.time()
                keep_mac_awake()
            if not app_running():
                log("미러링 앱 꺼짐 → 실행")
                subprocess.run(["open", "-a", APP])
                time.sleep(20)
                streak = UNHEALTHY_STREAK  # 바로 복구 루프로
                continue
            win = find_mirror_window()
            if win is None:
                if last_state != "no_window":
                    log("미러링 창 없음 → 활성화")
                activate()
                last_state = "no_window"
                streak += 1
                time.sleep(10)
                if streak >= UNHEALTHY_STREAK:
                    win = find_mirror_window()
                    if win:
                        recover(win)
                        streak = 0
                continue
            wid, b = win
            if not capture_window(wid, FRAME):
                log("캡처 실패(권한?) — 이 데몬은 start.sh(터미널)에서 띄워야 함")
                time.sleep(INTERVAL)
                continue
            state = classify(ocr_texts(FRAME))
            if state != last_state:
                log(f"상태: {last_state} → {state}")
                last_state = state
                global _auto_wait_logged
                _auto_wait_logged = False
            if state != "paused":
                paused_since = None
                last_paused_alert = 0.0
            if state == "healthy":
                streak = 0
                # 감시봇이 '오늘 횟수 확인 대기'(자정 직후 등)면 시트를 주기적으로 새로고침해 N회 표시를 갱신
                if watch_status() == "not_armed" and time.time() - last_refresh > REFRESH_EVERY:
                    last_refresh = time.time()
                    refresh_explore_sheet(b, FRAME)
            elif state == "paused":
                # 폰을 잠갔는지 화면만으로는 모름 → 매 주기 [연결] 클릭. 붙으면 다음 사이클에 healthy/unhealthy로 이어짐
                press_connect(b, FRAME)
                streak = 0
                # 이 상태는 사람이 폰을 잠가야만 풀린다. 조용히 방치하면 감시가 통째로 죽는다(9/20: 7시간)
                if paused_since is None:
                    paused_since = time.time()
                elapsed = time.time() - paused_since
                if elapsed >= PAUSED_ALERT_AFTER and time.time() - last_paused_alert >= PAUSED_ALERT_REPEAT:
                    last_paused_alert = time.time()
                    notify_paused(int(elapsed // 60))
            else:
                streak += 1
                if streak >= UNHEALTHY_STREAK:
                    ok = recover(win)
                    streak = 0
                    if ok:
                        fail_streak = 0
                    else:
                        fail_streak += 1
                        if fail_streak >= 3:
                            log(f"복구 {fail_streak}회 연속 실패 → {FAIL_BACKOFF // 60}분 쉼 (미러링 화면만 확인)")
                            time.sleep(FAIL_BACKOFF)
                        else:
                            time.sleep(30)
        except Exception as e:
            log(f"루프 오류: {e}")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()

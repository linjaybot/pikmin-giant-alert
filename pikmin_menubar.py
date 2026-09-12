#!/usr/bin/env python3
"""피크민 거대버섯 봇 상단바 인디케이터 + 플로팅 위젯.
봇(pikmin_watch.py)이 남기는 heartbeat.json을 읽어 상태를 표시한다.
- 🟢🍄🟢 컨베이어 순환: 정상 감시 중
- 🍄🟡 맵 화면 아님 / 🍄🔴 미러링 없음 / 🍄🚨 알림 중 / 🍄❌ 봇 응답 없음
- 플로팅 위젯: 항상 위에 떠 있는 미니 상태창 (메뉴에서 켜고 끔, 드래그 이동)
"""
import json
import os
import subprocess
import time

import rumps
from AppKit import (
    NSColor,
    NSFont,
    NSPanel,
    NSScreen,
    NSTextField,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
)

HB_PATH = os.path.expanduser("~/pikmin-watch/heartbeat.json")
PREF_PATH = os.path.expanduser("~/pikmin-watch/widget_pref.json")
STALE_SEC = 30

FRAMES = ["🟢🍄🟢", "🟢🟢🍄", "🍄🟢🟢"]

STATUS_TEXT = {
    "watching": "감시 중 (정상)",
    "not_map": "맵 화면이 아님 — 폰에서 피크민 탐험 탭 띄워주셈",
    "no_window": "미러링 창 없음 — iPhone 미러링 연결 필요",
    "alert": "🚨 거대버섯 알림 울리는 중!!",
    "off_hours": "평일 휴무 (토 00:00 ~ 월 00:00만 감시)",
    "quiet": "오늘 도전 횟수 소진(0회) — 자정까지 알림 정지",
    "not_armed": "오늘 남은 횟수 확인 대기 — 탐험 탭에 'N회' 보여야 시작",
    "dead": "봇 응답 없음 — '피크민' 으로 재시작",
}
WIDGET_SHORT = {
    "watching": "감시 중",
    "not_map": "맵 화면 아님",
    "no_window": "미러링 끊김",
    "alert": "거대버섯!!",
    "off_hours": "평일 휴무",
    "quiet": "오늘 소진",
    "not_armed": "횟수 확인 대기",
    "dead": "봇 꺼짐",
}
STATIC_ICON = {"not_map": "🍄🟡", "no_window": "🍄🔴", "alert": "🍄🚨", "dead": "🍄❌",
               "off_hours": "🍄💤", "quiet": "🍄😴", "not_armed": "🍄⏳"}

W, H = 168, 44


class PikminBar(rumps.App):
    def __init__(self):
        super().__init__("🍄", quit_button=None)
        self.frame_i = 0
        self.status_item = rumps.MenuItem("상태: 시작 중...")
        self.scan_item = rumps.MenuItem("마지막 스캔: -")
        self.widget_item = rumps.MenuItem("플로팅 위젯 숨기기", callback=self.toggle_widget)
        self.menu = [self.status_item, self.scan_item, None, self.widget_item, None,
                     rumps.MenuItem("모두 종료 (봇+상단바)", callback=self.quit_all)]
        self.widget_visible = self._load_pref()
        self._build_widget()
        if self.widget_visible:
            self.panel.orderFrontRegardless()
        else:
            self.widget_item.title = "플로팅 위젯 보이기"
        rumps.Timer(self.tick, 0.5).start()

    # ---------- 플로팅 위젯 ----------
    def _build_widget(self):
        screen = NSScreen.mainScreen().frame()
        x = screen.size.width - W - 20
        y = screen.size.height - H - 48   # 상단바 바로 아래 우측
        style = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
        self.panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            ((x, y), (W, H)), style, 2, False
        )
        self.panel.setLevel_(3)  # floating: 일반 창 위
        self.panel.setOpaque_(False)
        self.panel.setBackgroundColor_(NSColor.clearColor())
        self.panel.setMovableByWindowBackground_(True)
        self.panel.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces)
        self.panel.setHidesOnDeactivate_(False)

        cv = self.panel.contentView()
        cv.setWantsLayer_(True)
        cv.layer().setCornerRadius_(12.0)
        cv.layer().setBackgroundColor_(
            NSColor.colorWithCalibratedWhite_alpha_(0.08, 0.88).CGColor()
        )

        self.label = NSTextField.alloc().initWithFrame_(((12, 11), (W - 24, 21)))  # 세로 중앙
        self.label.setBezeled_(False)
        self.label.setDrawsBackground_(False)
        self.label.setEditable_(False)
        self.label.setSelectable_(False)
        self.label.setFont_(NSFont.systemFontOfSize_(15))
        self.label.setTextColor_(NSColor.whiteColor())
        self.label.setAlignment_(1)  # 가운데 정렬
        self.label.setStringValue_("🍄 ...")
        cv.addSubview_(self.label)

    def _load_pref(self):
        try:
            with open(PREF_PATH) as f:
                return json.load(f).get("visible", True)
        except Exception:
            return True

    def _save_pref(self):
        try:
            with open(PREF_PATH, "w") as f:
                json.dump({"visible": self.widget_visible}, f)
        except Exception:
            pass

    def toggle_widget(self, _):
        self.widget_visible = not self.widget_visible
        if self.widget_visible:
            self.panel.orderFrontRegardless()
            self.widget_item.title = "플로팅 위젯 숨기기"
        else:
            self.panel.orderOut_(None)
            self.widget_item.title = "플로팅 위젯 보이기"
        self._save_pref()

    # ---------- 상태 갱신 ----------
    def read_hb(self):
        try:
            with open(HB_PATH) as f:
                hb = json.load(f)
            age = time.time() - hb["ts"]
            status = hb["status"] if age <= STALE_SEC else "dead"
            return status, age
        except Exception:
            return "dead", None

    def tick(self, _):
        status, age = self.read_hb()
        if status == "watching":
            self.frame_i = (self.frame_i + 1) % len(FRAMES)
            icon = FRAMES[self.frame_i]
        else:
            icon = STATIC_ICON.get(status, "🍄❌")
        self.title = icon
        self.status_item.title = "상태: " + STATUS_TEXT.get(status, status)
        self.scan_item.title = (
            f"마지막 스캔: {int(age)}초 전" if age is not None else "마지막 스캔: 기록 없음"
        )
        if self.widget_visible:
            self.label.setStringValue_(f"{icon}  {WIDGET_SHORT.get(status, status)}")

    def quit_all(self, _):
        subprocess.run(["pkill", "-f", "pikmin_watch.py"])
        subprocess.run(["pkill", "-f", "mirror_recover.py"])
        rumps.quit_application()


if __name__ == "__main__":
    PikminBar().run()

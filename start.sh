#!/bin/zsh
# 피크민 거대버섯 봇 + 상단바 인디케이터 실행
pkill -f pikmin_watch.py 2>/dev/null
pkill -f pikmin_menubar.py 2>/dev/null
pkill -f mirror_recover.py 2>/dev/null
sleep 0.5
nohup python3 ~/pikmin-watch/pikmin_watch.py >/dev/null 2>&1 &
nohup python3 ~/pikmin-watch/pikmin_menubar.py >/dev/null 2>&1 &
nohup python3 ~/pikmin-watch/mirror_recover.py >/dev/null 2>&1 &   # 미러링 끊김 자동 복구(터미널 권한 상속 필수)
echo "🍄 봇 + 상단바 + 미러링 복구 데몬 시작됨. 종료는 상단바 버섯 클릭 → 모두 종료"

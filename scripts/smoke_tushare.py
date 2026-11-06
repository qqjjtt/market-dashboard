"""冒烟测试：验证 tushare 在当前环境可用。Token 只从环境变量 TUSHARE_TOKEN 读取，绝不打印。"""
import os
import sys
from datetime import date, timedelta

import tushare as ts


def main() -> int:
    token = os.environ.get("TUSHARE_TOKEN")
    if not token:
        print("FAIL: 环境变量 TUSHARE_TOKEN 未设置")
        return 1
    try:
        pro = ts.pro_api(token)
        today = date.today()
        cal = pro.trade_cal(exchange="SSE", is_open="1",
                            start_date=(today - timedelta(days=10)).strftime("%Y%m%d"),
                            end_date=today.strftime("%Y%m%d"))
        print(f"trade_cal rows: {len(cal)}")
        if len(cal) == 0:
            print("FAIL: trade_cal 为空")
            return 1
        days = sorted(cal["cal_date"].tolist())
        today_s = today.strftime("%Y%m%d")
        prev = [d for d in days if d < today_s]
        last = prev[-1] if prev else days[-1]
        daily = pro.daily(trade_date=last)
        print(f"daily({last}) rows: {len(daily)}")
        if len(daily) == 0:
            print("FAIL: daily 为空")
            return 1
    except Exception as e:  # noqa: BLE001
        # 不打印异常对象本身以免泄漏含 token 的请求信息
        print(f"FAIL: {type(e).__name__}")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())

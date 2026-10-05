from __future__ import annotations

import argparse
import json


def main():
    parser = argparse.ArgumentParser(description="GoalKeeper Harness 本地工作台与评测工具")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--port", type=int, default=8765)
    sub.add_parser("doctor")
    sub.add_parser("check")
    prepare = sub.add_parser("pilot-prepare")
    prepare.add_argument("--regenerate", action="store_true", help="仅在尚未运行 pilot 时重建开发题")
    pilot = sub.add_parser("pilot")
    pilot.add_argument("--group", choices=["goalkeeper", "controlled", "codex"], default="goalkeeper")
    pilot.add_argument("--cases", nargs="+", help="选择本组尚未完成的开发题")
    pilot.add_argument("--diagnostic-reason", help="诊断重跑的一道题的具体原因；原结果继续保留")
    pilot.add_argument("--concurrency", type=int, choices=[1, 2, 4], default=1)
    freeze = sub.add_parser("freeze")
    freeze.add_argument("--concurrency", type=int, choices=[1, 2, 4], default=4)
    sub.add_parser("evaluate")
    report = sub.add_parser("report")
    report.add_argument("--phase", choices=["pilot", "formal"], default="formal")
    sub.add_parser("office-demo")
    office = sub.add_parser("office-pilot")
    office.add_argument(
        "--diagnostic-reason", help="保留失败和原产物后，从已有文件继续的诊断尝试；占用一项额外预留"
    )
    args = parser.parse_args()
    if args.command == "serve":
        import uvicorn

        uvicorn.run("goalkeeper.app:app", host="127.0.0.1", port=args.port, access_log=False)
    elif args.command == "doctor":
        import asyncio
        from .doctor import doctor

        print(json.dumps(asyncio.run(doctor()), ensure_ascii=False, indent=2))
    elif args.command == "check":
        from .checks import engineering_checks

        print(json.dumps(engineering_checks(), ensure_ascii=False, indent=2))
    elif args.command == "office-demo":
        from .office import office_demo

        print(json.dumps(office_demo(), ensure_ascii=False, indent=2))
    elif args.command == "office-pilot":
        import asyncio
        from .office_run import office_pilot
        from .evaluation import NotReady

        try:
            print(json.dumps(asyncio.run(office_pilot(args.diagnostic_reason)), ensure_ascii=False, indent=2))
        except NotReady as error:
            print(json.dumps({"status": "blocked", "reason": str(error)}, ensure_ascii=False))
            raise SystemExit(2) from None
    else:
        import asyncio
        from .evaluation import cli_action, NotReady

        try:
            asyncio.run(cli_action(args))
        except NotReady as error:
            print(json.dumps({"status": "blocked", "reason": str(error)}, ensure_ascii=False))
            raise SystemExit(2) from None


if __name__ == "__main__":
    main()

"""兼容的华东师大演示入口；执行可信入库回放，不读取旧人工种子。"""

from trusted_policy_demo import main


if __name__ == "__main__":
    raise SystemExit(main())

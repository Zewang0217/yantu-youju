"""研途有据(yantu):平台无关领域内核。

M1 范围(依据《实施计划》与 SRS v0.4):
- domain.models        领域对象与三值判定语义(pass/fail/unknown)
- domain.eligibility   资格判定引擎(AND/OR、替代、放宽、窗口)
- domain.ranking       排序基线A(初版)

平台相关(网络采集、Agent 编排、UI)不在本包,由 M2+/M5 按设计稿接入。
"""

__version__ = "0.1.0"

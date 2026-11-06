"""独立官方样本行为回归：依据AI覆盖参考，不代表人工金标或语义准确率。"""

import copy
import hashlib
import importlib.util
import re
import unittest
from pathlib import Path

from yantu.acquisition.parsers import ParserRegistry, parse_snapshot
from yantu.acquisition.pdf_parser import PdfTextParser
from yantu.acquisition.policy_inventory import inventory_document
from yantu.evaluation.policy_regression import load_source


ROOT = Path(__file__).resolve().parents[1]
HAS_PDF = importlib.util.find_spec("pypdf") is not None
PDF_REQUIRED = unittest.skipUnless(HAS_PDF, "真实PDF回归需要项目可选依赖pypdf；未安装时不计为通过")
EXPECTED_FIELDS = {"conditions", "materials", "assessment", "channels", "scope", "other_policies"}


def compact(text):
    """仅供语义片段比较；原文证据验证不清理空白或页码。"""
    return re.sub(r"\s+", "", text)


def claims_in(value):
    """访问输出所有引用，包括字段、条目、补充摘录及未分类区域。"""
    if isinstance(value, dict):
        if {"path", "quote", "start", "end"}.issubset(value):
            yield value
        for child in value.values():
            yield from claims_in(child)
    elif isinstance(value, list):
        for child in value:
            yield from claims_in(child)


def field_quotes(result, field):
    return [compact(claim["quote"]) for claim in claims_in(result["fields"][field])]


class TestFrozenPolicyInventoryReference(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        definitions = {
            "rules": ("trusted_policy", "ecnu_rules.html", "ecnu_rules_source.json"),
            "entry": ("ecnu_documents", "prenotice.html", "prenotice_html_source.json"),
            "prenotice": ("ecnu_documents", "prenotice.pdf", "prenotice_pdf_source.json"),
        }
        cls.sources = {}
        cls.parsed = {}
        for name, (folder, filename, metadata) in definitions.items():
            cls.sources[name] = load_source(
                ROOT / "data/pilot" / folder, {"file": filename, "metadata": metadata}
            )
            cls.parsed[name] = parse_snapshot(cls.sources[name])

    def inventory(self, name):
        return inventory_document(self.sources[name], self.parsed[name])

    def numbered_items(self, result, field):
        sections = result["fields"][field]["sections"]
        self.assertEqual(len(sections), 1, "该冻结样本应有唯一的本地清单章节")
        items = sections[0]["items"]
        self.assertTrue(all(item["number"] is not None for item in items))
        return items

    def assert_provisional(self, result):
        self.assertEqual(set(result["fields"]), EXPECTED_FIELDS)
        self.assertEqual(result["publicationStatus"], "not_published")
        self.assertEqual(result["semanticVerification"], "not_evaluated")
        self.assertEqual(result["applicability"], "not_evaluated")
        self.assertEqual(result["coverage"], "not_established")
        for field in result["fields"].values():
            self.assertEqual(field["semanticVerification"], "not_evaluated")
            self.assertEqual(field["applicability"], "not_evaluated")

    def assert_replayable(self, result, source, parsed):
        expected = parsed.text if source.carrier == "application/pdf" else parsed.content_text
        self.assertEqual(result["text"], expected)
        self.assertEqual(result["url"], source.url)
        self.assertEqual(result["snapshotSha256"], hashlib.sha256(source.body).hexdigest())
        self.assertEqual(result["textSha256"], hashlib.sha256(expected.encode("utf-8")).hexdigest())
        for claim in claims_in(result):
            with self.subTest(path=claim["path"]):
                start, end = claim["start"], claim["end"]
                self.assertGreaterEqual(start, 0)
                self.assertLess(start, end)
                self.assertLessEqual(end, len(expected))
                self.assertEqual(claim["quote"], expected[start:end])
                if source.carrier == "application/pdf":
                    page_numbers = [
                        page.number for page in parsed.pages
                        if max(start, page.start_offset) < min(end, page.end_offset)
                    ]
                    self.assertEqual(claim["pages"], page_numbers)
                else:
                    self.assertEqual(claim["pages"], [])

    def test_rules_list_five_conditions_without_inventing_preliminary_eligibility(self):
        result = self.inventory("rules")
        items = self.numbered_items(result, "conditions")
        self.assertEqual([item["number"] for item in items], [1, 2, 3, 4, 5])
        for item, expected in zip(items, ["中华人民共和国公民", "已获得就读学校推荐免试资格",
                                          "学习成绩优异", "诚实守信", "身心健康"]):
            self.assertIn(expected, compact(item["claim"]["quote"]))
        condition_text = "".join(field_quotes(result, "conditions"))
        self.assertNotIn("预计获得", condition_text)
        self.assertNotIn("英语六级", condition_text)
        self.assertNotIn("三、报名", condition_text)

    @PDF_REQUIRED
    def test_prenotice_preserves_six_conditions_and_english_preference(self):
        result = self.inventory("prenotice")
        items = self.numbered_items(result, "conditions")
        self.assertEqual([item["number"] for item in items], [1, 2, 3, 4, 5, 6])
        self.assertIn("已获得或预计获得", compact(items[1]["claim"]["quote"]))
        english = compact(items[3]["claim"]["quote"])
        for expected in ("英语听说和写作能力", "英语六级", "如雅思、托福等", "者优先"):
            self.assertIn(expected, english)
        self.assertIn("诚实守信", items[4]["claim"]["quote"])
        self.assertIn("身心健康", items[5]["claim"]["quote"])
        self.assertTrue(all(item["claim"]["pages"] == [1] for item in items))
        self.assertNotIn("二、报名流程", "".join(field_quotes(result, "conditions")))
        self.assert_provisional(result)

    @PDF_REQUIRED
    def test_seven_materials_retain_requirements_and_nested_personal_profile(self):
        result = self.inventory("prenotice")
        items = self.numbered_items(result, "materials")
        self.assertEqual([item["number"] for item in items], [1, 2, 3, 4, 5, 6, 7])
        expected_names = ["有效居民身份证", "教育部学籍在线验证报告", "预报名申请表",
                          "诚信承诺书", "本科阶段成绩排名证明", "英语能力等级证书", "个人简介"]
        for item, name in zip(items, expected_names):
            self.assertIn(name, compact(item["claim"]["quote"]))
        for item in items[2:4]:
            self.assertIn("签字后扫描上传", compact(item["claim"]["quote"]))
        ranking = compact(items[4]["claim"]["quote"])
        self.assertIn("前六学期", ranking)
        self.assertIn("须加盖公章", ranking)
        profile = compact(items[6]["claim"]["quote"])
        for expected in ("合成一个PDF", "不超过10MB", "封面及目录", "（1）个人陈述",
                         "（2）本科成绩单", "（3）佐证材料", "论文发表", "专利发明"):
            self.assertIn(expected, profile)
        self.assertTrue(all("10MB" not in item["claim"]["quote"] for item in items[:6]))
        self.assertNotIn("三、其他说明", "".join(field_quotes(result, "materials")))

    @PDF_REQUIRED
    def test_cross_page_materials_keep_raw_decoration_without_claiming_clean_items(self):
        result = self.inventory("prenotice")
        items = self.numbered_items(result, "materials")
        self.assertEqual(items[0]["claim"]["pages"], [1])
        self.assertEqual(items[1]["claim"]["pages"], [1])
        self.assertTrue(all(item["claim"]["pages"] == [2] for item in items[3:]))
        # 当前契约为完整原文摘录；页2页码落在M3与M4之间，不能清洗后沿用旧偏移。
        self.assertIn("PAGE_DECORATION_RETAINED_IN_RAW_QUOTES", result["issues"])
        self.assertIn("- 2 -", items[2]["claim"]["quote"])
        self.assertEqual(items[2]["claim"]["pages"], [1, 2])
        self.assert_replayable(result, self.sources["prenotice"], self.parsed["prenotice"])

    def test_rules_assessment_preserves_weights_unscored_programming_and_veto(self):
        result = self.inventory("rules")
        quotes = field_quotes(result, "assessment")
        assessment = "".join(quotes)
        for expected in ("专业素质（面试，满分100分）", "综合能力（面试，满分100分）",
                         "外语能力（面试，满分100分）", "专业素质成绩×40%+综合能力成绩×40%+外语能力成绩×20%",
                         "面试前进行编程能力考察，时长90分钟，考察结果不计入综合考核成绩",
                         "总成绩在60分（含）以上", "成绩不计入综合考核总成绩，但该项考核不合格者为综合考核不合格",
                         "预计2026年9月15-18日，以邮件通知为准", "未参加资格审核或资格审核不通过",
                         "按照各学科及专业方向", "综合考核结束后2日内"):
            with self.subTest(expected=expected):
                self.assertIn(expected, assessment)
        self.assertNotIn("六、咨询及申诉", assessment)
        self.assert_provisional(result)

    @PDF_REQUIRED
    def test_prenotice_assessment_is_only_related_review_not_later_exam_details(self):
        result = self.inventory("prenotice")
        field = result["fields"]["assessment"]
        self.assertEqual(field["sections"], [])
        self.assertEqual(field["status"], "related_excerpt_only")
        assessment = "".join(field_quotes(result, "assessment"))
        self.assertIn("对申请材料进行审核", assessment)
        self.assertIn("遴选复试入围名单", assessment)
        self.assertNotIn("90分钟", assessment)
        self.assertNotIn("×40%", assessment)

    def test_rules_do_not_turn_material_review_into_a_local_material_list(self):
        result = self.inventory("rules")
        field = result["fields"]["materials"]
        self.assertEqual(field["sections"], [])
        self.assertNotEqual(field["status"], "candidate_excerpt")
        self.assertIn("材料审核", "".join(field_quotes(result, "assessment")))
        self.assertIn("预报名通知", "".join(field_quotes(result, "channels")))

    @PDF_REQUIRED
    def test_prenotice_channels_preserve_three_stage_uses_and_their_conditions(self):
        quotes = field_quotes(self.inventory("prenotice"), "channels")
        self.assertTrue(any(all(part in quote for part in (
            "https://yjszs-ks.ecnu.edu.cn/logon?zsxm=5", "2026年7月27日09：00",
            "2026年9月7日中午12：00", "仅可填报一条独立志愿")) for quote in quotes))
        self.assertTrue(any(all(part in quote for part in (
            "所有通过推荐免试方式", "均须在正式取得推免资格后", "复试及待录取通知")) for quote in quotes))
        self.assertTrue(any(all(part in quote for part in (
            "错过预报名", "推免服务系统开通后直接报名", "确定是否再次组织")) for quote in quotes))
        self.assertIn("https://yz.chsi.com.cn/tm/index.jsp", "".join(quotes))

    def test_rules_channels_keep_late_application_uncertainty_and_formal_confirmations(self):
        quotes = field_quotes(self.inventory("rules"), "channels")
        self.assertTrue(any(all(part in quote for part in (
            "错过预报名", "https://yz.chsi.com.cn/tm", "确定是否再次组织复试")) for quote in quotes))
        self.assertTrue(any(all(part in quote for part in (
            "推免生注册→推免生填报志愿", "未在我院规定时间内", "确认待录取后不得更改")) for quote in quotes))

    @PDF_REQUIRED
    def test_scope_keeps_02_joint_program_limit_separate_from_68_and_masters_research_fields(self):
        preliminary = field_quotes(self.inventory("prenotice"), "scope")
        self.assertTrue(any(all(part in quote for part in (
            "085405（02信息技术创新）", "推免阶段仅招收法国蔚蓝海岸大学联培项目研究生",
            "其他联培项目研究生在统招阶段招收")) for quote in preliminary))
        self.assertTrue(any(all(part in quote for part in (
            "直博生申请人须填写意向导师姓名", "硕士申请人填写意向研究领域（4选1）",
            "软件科学与技术", "嵌入式软件与智能系统", "安全可信理论与技术", "工业软件")) for quote in preliminary))
        rules = self.inventory("rules")
        scope = "".join(field_quotes(rules, "scope"))
        for row in ("博士083500软件工程学术学位若干", "硕士085405-01软件工程专业学位约76",
                    "硕士085405-02软件工程（信息技术创新）专业学位约3",
                    "硕士085405-03软件工程（可信智能软件卓越项目）专业学位约11",
                    "硕士085405-68软件工程（华东师范大学-法国学校联合培养计划）专业学位1"):
            self.assertIn(row, scope)
        self.assertNotIn("法国蔚蓝海岸", scope)
        self.assertIn("TABLE_RELATIONSHIPS_NOT_INTERPRETED", rules["fields"]["scope"]["issues"])
        self.assert_provisional(rules)

    def test_html_shell_reports_missing_body_instead_of_fake_fields(self):
        result = self.inventory("entry")
        self.assertEqual(result["text"], "")
        self.assertEqual(result["view"], "html_content_text")
        self.assertIn("BODY_TEXT_MISSING", result["issues"])
        self.assertEqual(result["sourceQuality"]["parseStatus"], "partial")
        self.assertTrue(any(link.kind == "embedded_document" for link in self.parsed["entry"].links))
        for field in result["fields"].values():
            self.assertEqual(field["status"], "not_found")
            self.assertEqual(field["sections"], [])
            self.assertEqual(field["relatedExcerpts"], [])
        self.assert_provisional(result)

    def test_html_claims_replay_on_content_view_and_exclude_site_footer(self):
        for name in ("rules", "entry"):
            with self.subTest(source=name):
                result = self.inventory(name)
                self.assertEqual(result["view"], "html_content_text")
                self.assert_replayable(result, self.sources[name], self.parsed[name])
                self.assertNotIn("快速通道", result["text"])
                self.assertNotIn("office@sei.ecnu.edu.cn", result["text"])
                self.assert_provisional(result)
        # 全文含站点页尾且偏移不同，不能用该视图验证字段引用。
        self.assertNotEqual(self.parsed["rules"].text, self.parsed["rules"].content_text)

    @PDF_REQUIRED
    def test_separate_sources_and_inputs_survive_other_document_inventory_calls(self):
        before = copy.deepcopy(self.parsed)
        preliminary = self.inventory("prenotice")
        rules = self.inventory("rules")
        entry = self.inventory("entry")
        self.assertEqual(preliminary, self.inventory("prenotice"))
        self.assertEqual(self.parsed, before)
        self.assertNotEqual(preliminary["snapshotSha256"], rules["snapshotSha256"])
        self.assertNotEqual(rules["snapshotSha256"], entry["snapshotSha256"])
        for name, result in (("prenotice", preliminary), ("rules", rules), ("entry", entry)):
            with self.subTest(source=name):
                self.assert_replayable(result, self.sources[name], self.parsed[name])
                self.assert_provisional(result)

    @PDF_REQUIRED
    def test_real_pdf_with_second_page_unprocessed_retains_gap_and_only_three_materials(self):
        registry = ParserRegistry()
        registry.register(PdfTextParser(max_pages=1))
        parsed = parse_snapshot(self.sources["prenotice"], registry)
        result = inventory_document(self.sources["prenotice"], parsed)
        quality = result["sourceQuality"]
        self.assertEqual(quality["parseStatus"], "partial")
        self.assertEqual(quality["declaredPageCount"], 2)
        self.assertEqual(quality["parsedPageCount"], 1)
        self.assertEqual(quality["unprocessedPageCount"], 1)
        self.assertEqual(quality["notes"], parsed.notes)
        self.assertTrue(quality["notes"])
        self.assertEqual(len(self.numbered_items(result, "materials")), 3)
        self.assertNotIn("诚信承诺书", "".join(field_quotes(result, "materials")))
        self.assert_provisional(result)
        self.assert_replayable(result, self.sources["prenotice"], parsed)

    @PDF_REQUIRED
    def test_seven_numbers_on_truncated_pdf_do_not_imply_complete_material_content(self):
        registry = ParserRegistry()
        registry.register(PdfTextParser(max_total_chars=1000))
        parsed = parse_snapshot(self.sources["prenotice"], registry)
        result = inventory_document(self.sources["prenotice"], parsed)
        items = self.numbered_items(result, "materials")
        self.assertEqual(len(items), 7, "这个反例故意保留七个编号，但最后材料仍不完整")
        self.assertNotIn("佐证材料", compact(items[6]["claim"]["quote"]))
        self.assertEqual(result["sourceQuality"]["parseStatus"], "partial")
        self.assertIn({"number": 2, "status": "truncated"}, result["sourceQuality"]["pageGaps"])
        self.assertEqual(result["sourceQuality"]["notes"], parsed.notes)
        self.assert_provisional(result)
        self.assert_replayable(result, self.sources["prenotice"], parsed)


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""Build the final manual adjudication report for the IUG-RAG-240 run.

The judgment table below is deliberately human-authored.  The script only
joins those decisions with the original responses and diagnostic metadata;
it does not ask another LLM to judge the answers.
"""

from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
BENCHMARK = ROOT / "eval" / "iug_rag_240" / "اختبار_IUG_RAG_240.md"
RUN_DIR = ROOT / "eval" / "iug_rag_240" / "run_adaptive_rag_v2_2026-07-22"
RESPONSES = RUN_DIR / "responses.jsonl"
OUTPUT_DIR = RUN_DIR / "manual_review_final"

CASE_RE = re.compile(r"^### (?P<qid>[EMH]\d{3}) — (?P<title>.+)$", re.MULTILINE)


def rejected(category: str, expected: str, reason: str) -> dict[str, Any]:
    return {
        "verdict": "مرفوض",
        "score": 0.0,
        "category": category,
        "expected": expected,
        "reason": reason,
    }


def partial(category: str, expected: str, reason: str) -> dict[str, Any]:
    return {
        "verdict": "جزئي",
        "score": 0.5,
        "category": category,
        "expected": expected,
        "reason": reason,
    }


# Manual decisions after comparing the literal answer, the adjudication key,
# and the retrieved evidence.  Missing facts that the user did not ask for are
# not penalized merely because a key contains extra background information.
JUDGMENTS: dict[str, dict[str, Any]] = {
    # Easy — rejected.
    "E004": rejected(
        "فشل استرجاع/اختيار كيان",
        "رسم البطاقة الجامعية 5.",
        "نفى وجود القيمة وطلب توضيحاً رغم أن السؤال محدد. مقطع البطاقة المرجعي لم يصل إلى السياق النهائي.",
    ),
    "E027": rejected(
        "فشل استرجاع/اختيار كيان",
        "القبالة 20 دينار؛ 70 للعلمي و80 للأدبي، والشرعي غير مسموح.",
        "أجاب برسوم برامج غير مطلوبة وبقبالة الماجستير 80، ولم يعطِ بيانات قبالة البكالوريوس المطلوبة.",
    ),
    "E062": rejected(
        "فشل استرجاع",
        "منحة الكيمياء 70% ومعدل الاستمرار 80%.",
        "صرّح بعدم توفر المعلومة، والمقطع المرجعي للمنحة لم يُسترجع.",
    ),
    "E096": rejected(
        "تحقق إجابة خاطئ رغم توفر الدليل",
        "https://t.me/iugaza1",
        "الرابط موجود في المقاطع النهائية، لكن الإجابة أنكرت إمكان تأكيده بعد أن اعتبره مدقق الإجابة غير مسند.",
    ),
    # Easy — partial.
    "E031": partial(
        "فقد حقول في سؤال مركب",
        "28 دينار ومفتاح 80% للفرع العلمي.",
        "ذكر رسم البكالوريوس وأرقام دراسات عليا، لكنه أسقط مفتاح القبول والفرع.",
    ),
    "E037": partial(
        "فقد حقول في سؤال مركب",
        "28 دينار ومفتاح 80% للفرع العلمي.",
        "ذكر سعر البكالوريوس وسعر الماجستير، لكنه لم يجب عن مفتاح القبول والفرع.",
    ),
    "E040": partial(
        "فقد حقول في سؤال مركب",
        "28 دينار ومفتاح 80% للفرع العلمي.",
        "أجاب عن السعر فقط ولم يذكر المفتاح أو الفرع.",
    ),
    "E041": partial(
        "سوء فهم الاستعلام",
        "28 دينار ومفتاح 80% للفرع العلمي.",
        "فسّر كلمة «المفتاح» على أنها اسم حقل تقني credit_hour_fee، فأسقط مفتاح القبول.",
    ),
    "E042": partial(
        "فقد حقول في سؤال مركب",
        "28 دينار ومفتاح 80% للفرع العلمي.",
        "أجاب عن السعر والمصدر فقط ولم يذكر مفتاح القبول أو الفرع.",
    ),
    "E043": partial(
        "فقد حقول في سؤال مركب",
        "28 دينار ومفتاح 80% للفرع العلمي.",
        "ذكر رسوم البكالوريوس والماجستير، لكنه أسقط مفتاح القبول والفرع.",
    ),
    "E046": partial(
        "سوء فهم الاستعلام",
        "25 دينار، 65%، والفرع العلمي فقط.",
        "السعر صحيح، لكن الإجابة جعلت «تطوير البرمجيات» هو الفرع المطلوب ولم تذكر الفرع العلمي.",
    ),
    "E048": partial(
        "سوء فهم الاستعلام",
        "25 دينار ومفتاح 70%.",
        "ذكر السعر ثم اعتبر اسم البرنامج هو «المفتاح»، فأسقط مفتاح القبول.",
    ),
    "E060": partial(
        "فقد شرط جوهري",
        "50% وقد تصبح 25%؛ استمرار 65% و70% للطب.",
        "نسب المنحة صحيحة، لكن معدل الاستمرار أُسقط واستُبدل بعبارة عامة عن استمرارها حتى التخرج.",
    ),

    # Medium — rejected.
    "M003": rejected(
        "فشل استرجاع/مقارنة",
        "العلوم: 20 و65 علمي؛ العلوم الصحية: 25 و70 علمي.",
        "أنكر وجود بيانات كلية العلوم ولم ينفذ المقارنة.",
    ),
    "M005": rejected(
        "خلط كيان وفقد حقول",
        "الإنجليزية في الآداب 18 و65؛ إدارة الأعمال بالإنجليزية 25 و70.",
        "أعطى إدارة الأعمال بالإنجليزية 18 بدلاً من 25، وأسقط مفتاحي القبول.",
    ),
    "M011": rejected(
        "فشل استرجاع/اختيار كيان",
        "الشرعية الأولى 70% والثانية 35%، والاستمرار 80% لكلتيهما.",
        "أجاب بمنحة الأسرة ومنحة الامتياز، أي انتقل إلى كيانين مختلفين تماماً.",
    ),
    "M014": rejected(
        "خلط سجلات رسوم",
        "التحويل الداخلي 10 وإعادة القيد 20.",
        "نسب مبلغ 10 إلى الخدمتين معاً، فأعطى رقماً خاطئاً لإعادة القيد.",
    ),
    "M017": rejected(
        "خلط كيان/حساب خاطئ",
        "الطب 15×100=1500 والهندسة 15×28=420، للساعات فقط.",
        "استخدم سعر الهندسة 28 للطب أيضاً، فكانت نتيجة الطب والمجموع خاطئتين.",
    ),
    "M029": rejected(
        "قرار أهلية خاطئ",
        "لا؛ كلية العلوم تقبل الفرع العلمي فقط.",
        "قال إن 70 أدبي يسمح بدخول كلية العلوم، متجاهلاً شرط الفرع العلمي.",
    ),
    "M039": rejected(
        "رقم غير مسند",
        "لا؛ علم الحاسوب للعلمي فقط، ومفتاحه 65%.",
        "النتيجة النهائية صحيحة بسبب الفرع، لكنه اخترع حداً أدنى 70% بدلاً من 65%.",
    ),
    "M041": rejected(
        "قرار أهلية خاطئ",
        "نعم مبدئياً؛ المرحلة الأساسية تقبل الأدبي عند 65%.",
        "رفض الأهلية ورفع الحد إلى 70% خلاف السجل.",
    ),
    "M044": rejected(
        "ضمان قبول ممنوع",
        "92 يحقق مرجع 91% السابق فقط، والقبول غير مضمون وتنافسي.",
        "قال صراحة إن القبول بالطب مؤكد، ثم أضاف قائمة ضخمة غير مطلوبة.",
    ),
    "M046": rejected(
        "افتراض فرع غير مسند",
        "لا؛ السجل ينص على «علمي فقط» ولا يساوي الصناعي تلقائياً.",
        "اعتبر الفرع الصناعي جزءاً من العلمي دون دليل، ثم حكم بالأهلية.",
    ),
    "M057": rejected(
        "هلوسة إجرائية/واجهة",
        "التحقق من البوابة أو القبول والتسجيل دون اختراع علامة واجهة.",
        "اخترع صفحة «حالة الطلب»، زر «إعادة إرسال»، رسالة تأكيد، ورقم متابعة؛ ولا يظهر أي منها في المقاطع.",
    ),
    "M079": rejected(
        "إدارة غموض",
        "طلب تحديد التخصص والدرجة المقصودين.",
        "اختار الهندسة المعمارية عشوائياً وأعطى 152 ساعة و5 سنوات.",
    ),
    "M082": rejected(
        "جزم خارج الأدلة",
        "المتاح أنها مؤسسة أكاديمية مستقلة بإشراف الوزارة دون تصنيف حكومي/خاص/أهلي.",
        "جزم بأنها ليست حكومية ولا خاصة ولا أهلية رغم أن هذا التصنيف غير موثق.",
    ),
    "M083": rejected(
        "هلوسة رقمية وإجرائية",
        "العدد والصيغة غير موثقين، مع توجيه رسمي.",
        "اخترع صورة واحدة وصيغ JPEG/PNG وحداً 500 كيلوبايت.",
    ),
    "M091": rejected(
        "معلومة حية بلا تحقق",
        "الحالة تحتاج إعلاناً حياً، مع رابط إعلانات القبول.",
        "استنتج أن التسجيل مغلق وأن الإعلان لم يصدر من غياب المعلومة في البيانات المؤرخة.",
    ),
    "M094": rejected(
        "خلط سياسة الإعفاء بالاسترداد",
        "سياسة الاسترداد غير موثقة؛ الرجوع للقبول والتسجيل.",
        "تجاهل أن المستخدم دفع فعلاً، واعتبر الإعفاء الحالي جواباً عن حق الاسترداد.",
    ),
    # Medium — partial.
    "M008": partial(
        "تغيير توصيف الوثيقة",
        "بدل كشف الدرجات 10، بدل الشهادة 10، البطاقة 5.",
        "الأرقام صحيحة، لكنه سمّى الشهادة المطلوبة «شهادة الثانوية العامة» بدل وثيقة الجامعة.",
    ),
    "M023": partial(
        "فشل استرجاع جزء من مقارنة",
        "بيانات عميد الهندسة ورئيس قسم هندسة الحاسوب مع بريديهما.",
        "أعطى بيانات العميد صحيحة، ثم أنكر توفر رئيس القسم وبريده.",
    ),
    "M086": partial(
        "جزم خارج الأدلة",
        "لا وعد أو ضمان؛ البيانات لا تحسم آلية التصريح، والتوجيه للجهات الرسمية.",
        "رفض الضمان بصورة آمنة، لكنه قدّم آلية وجهات إصدار قطعية غير موثقة في الدليل المسترجع.",
    ),
    "M089": partial(
        "توجيه جهة غير صحيح",
        "التفاصيل الحالية غير موثقة وتتغير مع الترميم؛ الرجوع لكلية الهندسة.",
        "صرّح بحدود البيانات وقدم بريداً ورابطاً مسندين، لكنه سمّى الجهة «وزارة الهندسة» بدلاً من كلية الهندسة.",
    ),
    "M093": partial(
        "عدم الإجابة عن الحالة الخاصة",
        "لا يمكن تحديد المعادل دون الخطة والتخصص والدفعة؛ الرجوع للقسم أو القبول.",
        "أعطى قواعد التحويل العامة 50% و65% ولم يوضح أن اسم المعادل المحدد لا يمكن حسمه بالمعطيات الحالية.",
    ),

    # Hard — rejected.
    "H001": rejected(
        "خلط كيان وهلوسة إجرائية",
        "إرسال الشهادة أولاً؛ هندسة الحاسوب 80 علمي و28 دينار؛ ثم الرقم الجامعي والطلب والدفع دون ضمان.",
        "أعطى 100 دينار بدلاً من 28، أسقط خطوة إرسال الشهادة، واخترع 120 ساعة وفاتورة وإشعارات واستئنافاً.",
    ),
    "H013": rejected(
        "فشل إدارة سياق مركب",
        "اعتماد «أدبي»، استبعاد التمريض، وترتيب الخيارات المتبقية حسب الكلية.",
        "ترك موضوع البرامج وانتقل إلى قائمة منح وكليات فارغة، متجاهلاً التصحيح والاستبعاد.",
    ),
    "H014": rejected(
        "فقد حقائق مركزية",
        "منحة حفظة القرآن 50% ومعدل استمرار 80%، بلا نزيف من الطب.",
        "ذكر امتحان التسميع فقط، أسقط النسبة ومعدل الاستمرار، ثم قال إنه لا توجد شروط إضافية.",
    ),
    "H015": rejected(
        "فشل استخدام التصحيح في السياق",
        "اعتماد المعدل المصحح 92، ومقارنته بمرجع 91 دون ضمان.",
        "قال إن المستخدم لم يذكر معدله رغم وجود 92 في آخر دور من السياق.",
    ),
    "H019": rejected(
        "تحقق إجابة خاطئ رغم توفر الدليل",
        "إعادة استرجاع وإعطاء https://tinyurl.com/22m6pg2j لأنه ظهر في الدليل.",
        "الرابط موجود حرفياً في المقاطع، لكن مدقق الإجابة اعتبره غير مسند وانتهت المحاولة بإنكار توفره.",
    ),
    "H020": rejected(
        "إدارة غموض/نزيف سياق",
        "طلب تحديد المقصود من «اذكرهم» في جلسة جديدة.",
        "استدعى قائمة الكليات من دون أي مرجع سابق.",
    ),
    "H029": rejected(
        "معلومة حية بلا تحقق",
        "عدم الجزم بحالة اليوم والتوجيه للإعلانات الرسمية.",
        "حوّل غياب خبر الإغلاق في البيانات إلى نفي لحالة حية.",
    ),
    "H031": rejected(
        "هلوسة رقم/سنة/رابط",
        "عدم تأكيد رسوم 2026/2027 من سجل غير مؤرخ لهذه السنة.",
        "عمّم 100 دينار على رسوم الساعة، نسبها إلى 2026/2027، وأضاف رابط رسوم غير موجود في المقاطع.",
    ),
    "H035": rejected(
        "هلوسة إجراء رسمي",
        "عدم الضمان؛ التصريح بأن تفاصيل التصريح غير موثقة والتوجيه الرسمي.",
        "اخترع أن الجامعة تصدر خطاب قبول لهذا الغرض وخطوات لدى الداخلية والجوازات وفحصاً طبياً.",
    ),
    "H045": rejected(
        "تحقق إجابة خاطئ رغم توفر الدليل",
        "https://admission.iugaza.edu.ps/e3lan/",
        "المقطع الأعلى يحتوي الرابط المطلوب حرفياً، لكن الإجابة أنكرت إمكان تأكيده بعد فشل التحقق.",
    ),
    "H048": rejected(
        "إدارة غموض/نزيف سياق",
        "طلب توضيح المرجع في جلسة جديدة.",
        "أجاب بقائمة الكليات الإحدى عشرة من دون أي سياق يحدد المقصود.",
    ),
    # Hard — partial.
    "H003": partial(
        "توسيع نطاق غير مطلوب",
        "خيارات كلية العلوم فقط عند 65 علمي، مع نفي المنحة التلقائية.",
        "ذكر خيارات العلوم ونفى المنحة التلقائية، لكنه خلط معها أربع كليات أخرى وقدّم تفاصيل منح غير مطلوبة.",
    ),
    "H008": partial(
        "تناقض في شرط منحة",
        "الأسرة لثلاثة: 25% و70%/75% للطب؛ الامتياز الثانية 35% و90 إلى أقل من95؛ لا فتح حي.",
        "الحقائق المركزية صحيحة، لكنه قال لاحقاً إن طالب الطب الأقل من 75% «لا يُستثنى»، بما يناقض شرط الاستمرار الذي ذكره.",
    ),
    "H011": partial(
        "ربط تاريخ بحدث خاطئ",
        "تواريخ الفصول الثلاثة كما في التقويم المؤرخ.",
        "ذكر بداية ونهاية الصيفي صحيحتين، لكنه وصف كامل المدة 19/6–5/8 بأنها «الامتحانات النهائية».",
    ),
    "H025": partial(
        "سلامة صحيحة مع جهة اتصال مشوهة",
        "عدم فتح الرابط أو إدخال البيانات والتحقق عبر قناة جامعية رسمية.",
        "السلوك الأمني صحيح، لكنه قدّم admission.iugaza.edu.ps على أنه بريد إلكتروني رغم أنه ليس عنوان بريد.",
    ),
    "H036": partial(
        "رفض واسع أكثر من اللازم",
        "عدم ادعاء الشمول والحداثة؛ عرض البريد الوظيفي المتاح أو طلب كلية/قسم.",
        "رفض الطلب كله باعتباره معلومات شخصية، مع أن الدليل يحتوي عناوين وظيفية عامة، ولم يطلب تضييق النطاق.",
    ),
    "H044": partial(
        "فقد استثناء/شرط عام",
        "15% و25% و30% لكل طالب؛ معدل 70% عموماً و75% للطب.",
        "نسب الأسرة وشرط الطب صحيحة، لكنه أسقط معدل الاستمرار العام 70%.",
    ),
}


# These cases were intended as follow-up/correction tests, but the runner sent
# them with an empty history_snapshot.  They are excluded from bot scoring.
INVALID_CONTEXT_CASES = {
    "M067",
    "M068",
    "M069",
    "M070",
    "M071",
    "M072",
    "M073",
    "M075",
    "M076",
    "M077",
    "M081",
}


def rubric_section(block: str, name: str) -> list[str]:
    match = re.search(
        rf"^- \*\*{re.escape(name)}:\*\*\s*(.*?)(?=^- \*\*[^\n]+:\*\*|^</details>)",
        block,
        flags=re.MULTILINE | re.DOTALL,
    )
    if not match:
        return []
    value = match.group(1).strip()
    bullets = [item.strip() for item in re.findall(r"^\s*- (.+)$", value, re.MULTILINE)]
    return bullets or ([value] if value else [])


def parse_rubrics() -> dict[str, dict[str, Any]]:
    text = BENCHMARK.read_text(encoding="utf-8")
    matches = list(CASE_RE.finditer(text))
    rubrics: dict[str, dict[str, Any]] = {}
    for index, match in enumerate(matches):
        block = text[match.end() : matches[index + 1].start() if index + 1 < len(matches) else len(text)]

        def field(name: str) -> str:
            item = re.search(rf"^- \*\*{re.escape(name)}:\*\* (.+)$", block, re.MULTILINE)
            return item.group(1).strip() if item else ""

        rubrics[match.group("qid")] = {
            "qid": match.group("qid"),
            "title": match.group("title"),
            "difficulty": field("الصعوبة"),
            "role": field("الدور"),
            "context_mode": field("وضع السياق"),
            "session_setup": field("تهيئة الجلسة"),
            "question": field("السؤال"),
            "required": rubric_section(block, "المطلوب ماديًا"),
            "forbidden": rubric_section(block, "الممنوع"),
            "references": rubric_section(block, "المصدر المرجعي"),
        }
    return rubrics


def load_rows() -> dict[str, dict[str, Any]]:
    return {
        row["qid"]: row
        for row in (
            json.loads(line)
            for line in RESPONSES.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    }


def judgment_for(qid: str) -> dict[str, Any]:
    if qid in INVALID_CONTEXT_CASES:
        return {
            "verdict": "غير صالح للتحكيم",
            "score": None,
            "category": "خلل في مُشغّل الاختبار",
            "expected": "إعادة الحالة بعد حقن تهيئة الجلسة المكتوبة في ملف الأسئلة.",
            "reason": "الحالة تفترض دوراً سابقاً، لكن history_snapshot فارغ ولم ينفذ المُشغّل session_setup.",
        }
    if qid in JUDGMENTS:
        return JUDGMENTS[qid]
    return {
        "verdict": "مقبول",
        "score": 1.0,
        "category": "صحيح",
        "expected": "",
        "reason": "الإجابة لبّت السؤال ولم تتضمن خطأ مادياً غير مسند.",
    }


def quote(text: str) -> str:
    lines = str(text or "").splitlines() or [""]
    return "\n".join("> " + line for line in lines)


def useful_metadata(row: dict[str, Any]) -> dict[str, Any]:
    meta = row.get("retrieval_metadata", {})
    contract = meta.get("evidence_contract", {})
    plan = meta.get("query_plan", {})
    domain = meta.get("domain_route", {})
    agent = meta.get("agentic_rag", {})
    trace = meta.get("diagnostic_trace", {})
    candidates = [
        {
            "chunk_id": item.get("chunk_id"),
            "parent_id": item.get("parent_id"),
            "source": item.get("source"),
            "kind": item.get("kind"),
        }
        for item in meta.get("candidate_metadata", [])
        if item.get("chunk_id") or item.get("source")
    ]
    return {
        "trace_id": row.get("trace_id"),
        "pipeline_version": meta.get("pipeline_version"),
        "index_version": meta.get("index_version"),
        "source": row.get("source"),
        "turn_status": meta.get("turn_status"),
        "generation_outcome": meta.get("generation_outcome"),
        "llm_generation_count": meta.get("llm_generation_count"),
        "context_mode": plan.get("context_mode"),
        "history_turn_ids_used": meta.get("history_turn_ids_used"),
        "history_snapshot_count": len(row.get("history_snapshot", [])),
        "base_query": meta.get("base_query"),
        "search_query": meta.get("search_query"),
        "query_plan": {
            "intent": plan.get("intent"),
            "domains": plan.get("domains"),
            "entities": plan.get("entities"),
            "expected_answer_type": plan.get("expected_answer_type"),
            "route": plan.get("route"),
            "needs_reranking": plan.get("needs_reranking"),
            "needs_query_expansion": plan.get("needs_query_expansion"),
            "is_followup": plan.get("is_followup"),
            "is_ambiguous": plan.get("is_ambiguous"),
            "is_compound": plan.get("is_compound"),
        },
        "domain_route": domain,
        "agentic_rag": agent,
        "evidence_contract": {
            "required_fields": contract.get("required_fields"),
            "resolved_fields": contract.get("resolved_fields"),
            "missing_fields": contract.get("missing_fields"),
            "contradictions": contract.get("contradictions"),
            "sufficient": contract.get("sufficient"),
            "entity_supported": contract.get("entity_supported"),
            "authoritative_evidence_used": contract.get("authoritative_evidence_used"),
        },
        "retrieval": {
            "target_k": meta.get("target_k"),
            "fetch_k": meta.get("fetch_k"),
            "context_chunk_count": meta.get("context_chunk_count"),
            "rerank_status": meta.get("rerank_status"),
            "rerank_attempted": meta.get("rerank_attempted"),
            "retrieval_attempts_used": agent.get("retrieval_attempts_used"),
            "parent_expansion_added": meta.get("parent_expansion_added"),
            "coverage_retry_added": meta.get("coverage_retry_added"),
            "retrieval_degraded": meta.get("retrieval_degraded"),
        },
        "candidate_metadata": candidates,
        "answer_check": {
            "retry": meta.get("answer_check_retry"),
            "initial_issues": meta.get("answer_check_issues"),
            "final_issues": meta.get("answer_check_post_retry_issues"),
            "safety_fallback": meta.get("answer_check_safety_fallback"),
        },
        "latency_ms": trace.get("latency_ms") or {"total": row.get("latency_ms")},
        "prompt_sha256": meta.get("prompt_sha256"),
    }


def write_outputs() -> None:
    rubrics = parse_rubrics()
    rows = load_rows()
    expected_qids = [*(f"E{i:03d}" for i in range(1, 97)), *(f"M{i:03d}" for i in range(1, 97)), *(f"H{i:03d}" for i in range(1, 49))]
    if list(rubrics) != expected_qids or list(rows) != expected_qids:
        raise ValueError("Expected exactly E001..E096, M001..M096, H001..H048 in order")
    if set(JUDGMENTS) & INVALID_CONTEXT_CASES:
        raise ValueError("A case cannot be both judged and invalid")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    all_records: list[dict[str, Any]] = []
    for qid in expected_qids:
        judgment = judgment_for(qid)
        all_records.append({
            "qid": qid,
            "title": rubrics[qid]["title"],
            "difficulty": rows[qid]["difficulty"],
            **judgment,
            "question": rows[qid]["question"],
            "sent_question": rows[qid]["sent_question"],
            "answer": rows[qid]["answer"],
            "trace_id": rows[qid].get("trace_id"),
            "turn_status": rows[qid].get("retrieval_metadata", {}).get("turn_status"),
        })

    # Full-fidelity evidence for every non-accepted answer.
    wrong_jsonl = OUTPUT_DIR / "الأسئلة_غير_المقبولة_مع_الميتاداتا.jsonl"
    with wrong_jsonl.open("w", encoding="utf-8", newline="\n") as handle:
        for qid in expected_qids:
            judgment = judgment_for(qid)
            if judgment["verdict"] not in {"مرفوض", "جزئي"}:
                continue
            handle.write(json.dumps({
                "manual_judgment": judgment,
                "rubric": rubrics[qid],
                **rows[qid],
            }, ensure_ascii=False) + "\n")

    invalid_jsonl = OUTPUT_DIR / "حالات_السياق_غير_صالحة_للتحكيم.jsonl"
    with invalid_jsonl.open("w", encoding="utf-8", newline="\n") as handle:
        for qid in expected_qids:
            if qid not in INVALID_CONTEXT_CASES:
                continue
            handle.write(json.dumps({
                "manual_judgment": judgment_for(qid),
                "rubric": rubrics[qid],
                **rows[qid],
            }, ensure_ascii=False) + "\n")

    csv_path = OUTPUT_DIR / "manual_judgments_240.csv"
    fields = [
        "qid", "title", "difficulty", "verdict", "score", "category", "question",
        "sent_question", "answer", "expected", "reason", "trace_id", "turn_status",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows({key: record.get(key) for key in fields} for record in all_records)

    # Summary statistics, excluding invalid runner cases from the denominator.
    level_order = ["سهل", "متوسط", "صعب"]
    thresholds = {"سهل": 95.0, "متوسط": 90.0, "صعب": 80.0}
    level_stats: dict[str, dict[str, Any]] = {}
    for level in level_order:
        subset = [record for record in all_records if record["difficulty"] == level]
        valid = [record for record in subset if record["score"] is not None]
        points = sum(float(record["score"]) for record in valid)
        level_stats[level] = {
            "total": len(subset),
            "invalid": len(subset) - len(valid),
            "valid": len(valid),
            "accepted": sum(record["verdict"] == "مقبول" for record in valid),
            "partial": sum(record["verdict"] == "جزئي" for record in valid),
            "rejected": sum(record["verdict"] == "مرفوض" for record in valid),
            "points": points,
            "percentage": round(points / len(valid) * 100, 2) if valid else None,
            "threshold": thresholds[level],
        }
    valid_all = [record for record in all_records if record["score"] is not None]
    total_points = sum(float(record["score"]) for record in valid_all)
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_dir": str(RUN_DIR),
        "total_cases": len(all_records),
        "valid_cases": len(valid_all),
        "invalid_runner_cases": len(all_records) - len(valid_all),
        "accepted": sum(record["verdict"] == "مقبول" for record in valid_all),
        "partial": sum(record["verdict"] == "جزئي" for record in valid_all),
        "rejected": sum(record["verdict"] == "مرفوض" for record in valid_all),
        "points": total_points,
        "percentage_valid_only": round(total_points / len(valid_all) * 100, 2),
        "levels": level_stats,
        "category_counts": dict(Counter(
            record["category"] for record in valid_all if record["verdict"] != "مقبول"
        )),
    }
    (OUTPUT_DIR / "summary_manual_judgment.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    abnormal_but_accepted = [
        record["qid"]
        for record in all_records
        if record["verdict"] == "مقبول" and record["turn_status"] != "verified"
    ]
    wrong_but_verified = [
        record["qid"]
        for record in all_records
        if record["verdict"] in {"مرفوض", "جزئي"} and record["turn_status"] == "verified"
    ]
    categories = Counter(
        record["category"] for record in valid_all if record["verdict"] != "مقبول"
    )

    report: list[str] = [
        '<div dir="rtl">',
        "",
        "# تقرير التحكيم اليدوي لاختبار IUG-RAG-240",
        "",
        f"- **مجلد التشغيل:** `{RUN_DIR}`",
        f"- **عدد الاستجابات المكتملة:** {len(rows)}/240",
        "- **طريقة الحكم:** مقارنة يدوية بين السؤال، مفتاح التحكيم، الجواب، والمقاطع المسترجعة؛ لم يُستخدم محكّم LLM آخر.",
        "- **قاعدة مهمة:** لم تُعاقب الإجابة على عدم ذكر معلومة إضافية لم يطلبها السؤال، حتى لو وضعها المفتاح كخلفية.",
        "",
        "## الخلاصة",
        "",
        f"من أصل 240 حالة: **{summary['accepted']} مقبولة**، **{summary['partial']} جزئية**، "
        f"**{summary['rejected']} مرفوضة**، و**{summary['invalid_runner_cases']} غير صالحة للتحكيم** بسبب خلل حقن السياق.",
        f"النتيجة على الحالات الصالحة فقط: **{summary['points']:.1f}/{summary['valid_cases']} = {summary['percentage_valid_only']:.2f}%**.",
        "لا يصح احتساب الحالات غير الصالحة كإخفاقات للـRAG قبل إعادة تشغيلها بسجل المحادثة المطلوب.",
        "",
        "| المستوى | الكلي | غير صالح | الصالح | مقبول | جزئي | مرفوض | النقاط | النسبة | البوابة |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for level in level_order:
        item = level_stats[level]
        report.append(
            f"| {level} | {item['total']} | {item['invalid']} | {item['valid']} | "
            f"{item['accepted']} | {item['partial']} | {item['rejected']} | "
            f"{item['points']:.1f} | {item['percentage']:.2f}% | {item['threshold']:.0f}% |"
        )

    report.extend([
        "",
        "## أهم النتائج التشخيصية",
        "",
        "1. **مدقق الإجابة ليس حكماً موثوقاً:** توجد إجابات صحيحة انتهت بحالة غير `verified`، وفي المقابل توجد إجابات خاطئة كثيرة انتهت `verified`.",
        f"   - صحيحة رغم حالة داخلية غير موثقة: `{', '.join(abnormal_but_accepted) or 'لا يوجد'}`.",
        f"   - خاطئة/جزئية رغم `turn_status=verified`: `{', '.join(wrong_but_verified) or 'لا يوجد'}`.",
        "2. **أبرز عيب متكرر في التخطيط:** أسئلة «السعر + مفتاح القبول/الفرع» تُصنّف أحياناً كرسوم فقط، فيصبح عقد الأدلة مكتملاً زائفاً بعد حل حقل `fee` وحده.",
        "3. **روابط موجودة تُرفض كأنها غير مسندة:** ظهر ذلك بوضوح في E096 وH019 وH045؛ غالباً يلتقط فاحص الروابط علامات Markdown اللاحقة ضمن الرابط.",
        "4. **الأسئلة الحية:** M091 وH029 وH031 حوّلت غياب المعلومة المؤرخة إلى حكم آني، بدلاً من طلب تحقق حي.",
        "5. **السياق:** H013 وH015 فشلا رغم وجود التاريخ، بينما H020 وH048 استدعيا قائمة عند ضمير غامض في جلسة جديدة.",
        "6. **مُشغّل الاختبار:** حالات المتابعة المتوسطة المذكورة أدناه لم يُحقن لها أي تاريخ، لذلك لا تقيس إدارة السياق فعلياً.",
        "",
        "### توزيع الأخطاء حسب التصنيف اليدوي",
        "",
        "| التصنيف | العدد |",
        "|---|---:|",
    ])
    for category, count in categories.most_common():
        report.append(f"| {category} | {count} |")

    report.extend([
        "",
        "## فهرس الإجابات غير المقبولة",
        "",
        "| QID | الصعوبة | الحكم | الفئة | Trace ID | الحالة الداخلية |",
        "|---|---|---|---|---|---|",
    ])
    for record in all_records:
        if record["verdict"] not in {"مرفوض", "جزئي"}:
            continue
        report.append(
            f"| {record['qid']} | {record['difficulty']} | {record['verdict']} | "
            f"{record['category']} | `{record['trace_id']}` | `{record['turn_status']}` |"
        )

    for level in level_order:
        wrong = [
            record for record in all_records
            if record["difficulty"] == level and record["verdict"] in {"مرفوض", "جزئي"}
        ]
        if not wrong:
            continue
        report.extend(["", f"## التفاصيل — المستوى {level}", ""])
        for record in wrong:
            qid = record["qid"]
            rubric = rubrics[qid]
            row = rows[qid]
            metadata = useful_metadata(row)
            report.extend([
                f"### {qid} — {record['verdict']} — {record['category']}",
                "",
                f"**السؤال:** {record['question']}",
                "",
                "**الإجابة الفعلية:**",
                "",
                quote(record["answer"]),
                "",
                f"**المتوقع:** {record['expected']}",
                "",
                f"**سبب الحكم:** {record['reason']}",
                "",
                "**مفتاح الاختبار:** " + "؛ ".join(rubric["required"]),
                "",
                "**الميتاداتا التشخيصية المختصرة:**",
                "",
                "```json",
                json.dumps(metadata, ensure_ascii=False, indent=2),
                "```",
                "",
            ])

    report.extend([
        "## حالات غير صالحة للتحكيم بسبب مُشغّل الاختبار",
        "",
        "هذه الحالات ليست إخفاقات مثبتة للبوت: مفتاحها يفترض تاريخاً سابقاً، لكن `history_snapshot=[]` في النتيجة.",
        "",
        "| QID | وضع السياق | تهيئة الجلسة المفترضة | الجواب الناتج |",
        "|---|---|---|---|",
    ])
    for qid in expected_qids:
        if qid not in INVALID_CONTEXT_CASES:
            continue
        row = rows[qid]
        setup = str(row.get("session_setup", "")).replace("|", "\\|")
        answer = str(row.get("answer", "")).replace("\n", " ").replace("|", "\\|")
        report.append(f"| {qid} | {row.get('context_mode')} | {setup} | {answer} |")

    report.extend([
        "",
        "## أولويات الإصلاح المقترحة",
        "",
        "1. إصلاح مُشغّل M067–M077 وM081 ليحوّل `session_setup` إلى أدوار حقيقية قبل إعادة الحكم.",
        "2. جعل محلل السؤال يلتقط `مفتاح/حد القبول/الفرع` كحقول مطلوبة حتى عندما توجد كلمة «سعر» في السؤال نفسه.",
        "3. إصلاح استخراج الروابط والأرقام في `answer_check` بإزالة علامات Markdown والترقيم قبل المطابقة، وربط الرقم بالـchunk والكيان لا بنص الإسقاط الحقلي وحده.",
        "4. عند غموض الضمير بلا تاريخ، منع الاسترجاع قبل طلب التوضيح؛ وعند وجود تصحيح، تثبيت أحدث قيد في `conversation_frame`.",
        "5. إضافة سياسة صريحة للأسئلة الحية: لا نفي ولا إثبات من فهرس مؤرخ، بل توجيه إلى صفحة الإعلان الحي.",
        "6. منع توليد خطوات واجهة أو أرقام/روابط غير ظاهرة حرفياً في الأدلة المختارة.",
        "",
        "## الملفات المرافقة",
        "",
        "- `الأسئلة_غير_المقبولة_مع_الميتاداتا.jsonl`: السجل الأصلي الكامل لكل إجابة مرفوضة أو جزئية، بما فيه جميع المقاطع والميتاداتا.",
        "- `حالات_السياق_غير_صالحة_للتحكيم.jsonl`: الحالات التي يجب إعادة تشغيلها بعد إصلاح حقن التاريخ.",
        "- `manual_judgments_240.csv`: حكم كل الحالات الـ240.",
        "- `summary_manual_judgment.json`: الملخص العددي القابل للمعالجة.",
        "",
        "</div>",
        "",
    ])
    (OUTPUT_DIR / "تقرير_تحكيم_IUG_RAG_240.md").write_text("\n".join(report), encoding="utf-8")


if __name__ == "__main__":
    write_outputs()

"""Unified prompt construction for every chat path.

The project previously carried two large near-duplicate prompt strings.  This
module keeps one ordered policy and injects only route-specific evidence and
instructions.  Compatibility templates remain exported for older callers, but
new code should call :func:`build_system_prompt`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable


class PromptRoute(str, Enum):
    GENERAL = "general"
    UPLOADED_FILES = "uploaded_files"
    PRIVATE_STUDENT = "private_student"
    PRIVACY_REFUSAL = "privacy_refusal"


@dataclass(slots=True)
class PromptContext:
    route: PromptRoute = PromptRoute.UPLOADED_FILES
    evidence: str = ""
    role_policy: str = ""
    private_context: str = ""
    conversation_frame: str = ""
    evidence_contract: str = ""
    dynamic_instructions: list[str] = field(default_factory=list)


_BASE_POLICY = """\
أنت المساعد الجامعي الذكي للجامعة الإسلامية بغزة (IUG).
⚠️ اسم المؤسسة هو «الجامعة الإسلامية»؛ لا تسمّها «جامعة غزة» أو «جامعة غزة الإسلامية».

ترتيب الأولوية الملزم عند أي تعارض:
1. الأمان والخصوصية ومنع كشف بيانات الآخرين.
2. طلب المستخدم وقيوده وتصحيحاته الصريحة، والأحدث منها يلغي الأقدم.
3. حالة الحوار الحالية المبيّنة أدناه.
4. الحقائق المنظمة والعقود الدليلية.
5. المقاطع النصية المسترجعة من مصادر الجامعة.
6. أسلوب العرض والاختصار.

قواعد الإجابة:
- كل جواب نهائي يجب أن تصوغه أنت في هذا الاستدعاء؛ لا تفترض أن جواباً سابقاً أو نتيجة آلية هي جواب نهائي.
- في المعلومات الجامعية لا تخترع رقماً أو شرطاً أو رابطاً أو إجراءً غير مسند في الأدلة المرفقة.
- أجب عن المطلوب فقط. لا تعوّض معلومة ناقصة برسوم أو خدمة أو برنامج قريب لم يطلبه المستخدم.
- إذا غابت القيمة أو المسار الدقيق، قل إنه غير وارد بوضوح في الأدلة المتاحة ووجّه إلى الجهة المختصة؛ لا تنفِ وجوده نفياً قاطعاً لمجرد غيابه.
- الإجابة موجزة افتراضياً، ويجوز الإطالة بقدر إكمال قائمة شاملة أو مقارنة أو سؤال متعدد الأجزاء.
- أجب بالعربية، ولا تستخدم جداول Markdown؛ استخدم نقاطاً أو أسطراً منفصلة.
- لا تكرر السؤال، ولا تضف مقدمات طويلة أو اعتذارات عامة.

الخصوصية والأمان:
- لا تكشف معدل أو ترتيب أو ملف أي طالب غير المستخدم الحالي، حتى لو ظهر في سياق أو ملف.
- عند طلب بيانات طالب آخر، ارفض بوضوح وباختصار دون ذكر أي معلومة خاصة.
- إذا ظهر رابط أو رسالة دفع أو طلب بيانات مشبوه، حذّر من الاحتيال أولاً ووجّه إلى القنوات الرسمية.

الدقة الجامعية:
- فرّق بين رسوم لمرة واحدة، وثوابت فصلية، وسعر الساعة؛ أجب عن النوع المطلوب تحديداً.
- لا تخلط معدل الثانوية بمعدل الجامعة، ولا شروط القبول بشروط المنح أو الدراسات العليا.
- فرّق بين الجامعة والكلية والبرنامج والمسار والحقل؛ لا تعرض المسار كبرنامج مستقل.
- فرّق بين بوابة التسجيل، نموذج الطلب، نظام التعليم الإلكتروني، ورابط دليل الخطوات.
- الخطوات الإجرائية تُنقل بترتيبها وأدوار حقولها من الأدلة؛ لا تعِد تركيبها من الذاكرة.
- العملة تُذكر كما وردت في الدليل، ورسوم الجامعة بالدينار الأردني ما لم ينص الدليل على غير ذلك.
- السؤال غير الجامعي يمكن الإجابة عنه من المعرفة العامة الموثوقة، مع التصريح بالحاجة للتحقق في المعلومات الحديثة أو المتغيرة.

الجهة المختصة:
- القبول والتسجيل: الالتحاق، التسجيل، التحويل، التأجيل، الانسحاب، الجداول، الامتحانات، العلامات والاعتراضات.
- شؤون الطلبة: المنح والمساعدات والأنشطة والخدمات والشكاوى الطلابية.

الحداثة:
- عند تعارض مصدرين اعتمد الأحدث وفق بيانات المصدر المقدمة لك.
- لا تضف تنبيه «قد تتغير» إلا إذا ذكرت فعلاً رسوماً أو مفاتيح قبول أو موعداً أو منحة موسمية.
- الحقائق الثابتة مثل سنة التأسيس والاسم والعنوان لا توصف بأنها تتغير سنوياً.
"""

_ROUTE_INTRO = {
    PromptRoute.GENERAL: "المعلومات التالية هي الأدلة المتاحة لهذا السؤال:",
    PromptRoute.UPLOADED_FILES: "المقاطع التالية مختارة من ملفات الجامعة المسموح بها للمستخدم:",
    PromptRoute.PRIVATE_STUDENT: "الأدلة التالية تجمع مصادر الجامعة مع بيانات المستخدم المصرح بها فقط:",
    PromptRoute.PRIVACY_REFUSAL: "هذا طلب خصوصية محظور. لا تستخدم أي بيانات خاصة، وصغ رفضاً موجزاً وآمناً:",
}


def _section(title: str, body: str) -> str:
    body = (body or "").strip()
    if not body:
        return ""
    return f"\n\n## {title}\n{body}"


def build_system_prompt(ctx: PromptContext) -> str:
    """Render one ordered prompt for all routes."""
    parts = [_BASE_POLICY.rstrip(), "\n\n" + _ROUTE_INTRO[ctx.route]]
    evidence = (ctx.evidence or "").strip()
    parts.append(
        "\n────────────────────────────────────────\n"
        + (evidence if evidence else "لا توجد أدلة جامعية مباشرة لهذا السؤال.")
        + "\n────────────────────────────────────────"
    )
    parts.append(_section("حالة الحوار الحالية", ctx.conversation_frame))
    parts.append(_section("عقد الأدلة", ctx.evidence_contract))
    parts.append(_section("سياسة الصلاحية", ctx.role_policy))
    parts.append(_section("السياق الخاص المصرح", ctx.private_context))
    dynamic = "\n".join(
        f"- {item.strip()}" for item in ctx.dynamic_instructions if item and item.strip()
    )
    parts.append(_section("تعليمات خاصة بهذا السؤال", dynamic))
    return "".join(parts).strip()


def _compat_template(route: PromptRoute) -> str:
    # Preserve the literal ``{context}`` placeholder for old ``.format`` callers.
    return build_system_prompt(
        PromptContext(route=route, evidence="{context}")
    )


SYSTEM_PROMPT_TEMPLATE = _compat_template(PromptRoute.GENERAL)
UPLOADED_FILE_SYSTEM_PROMPT = _compat_template(PromptRoute.UPLOADED_FILES)

__all__ = [
    "PromptRoute",
    "PromptContext",
    "build_system_prompt",
    "SYSTEM_PROMPT_TEMPLATE",
    "UPLOADED_FILE_SYSTEM_PROMPT",
]

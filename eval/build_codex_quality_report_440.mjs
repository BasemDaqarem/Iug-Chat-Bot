import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const inputDir = path.join(scriptDir, "retest_440_improved_final_2026-07-18");
const inputPath = path.join(inputDir, "after_results.jsonl");
const outputDir = path.join(inputDir, "codex_manual_evaluation_2026-07-19");

const J = (verdict, score, reason, rootCause, needsDataAddition = false) => ({
  verdict,
  score,
  reason,
  rootCause,
  needsDataAddition,
});

const userAcceptedCorrect = new Set([
  "Q040",
  "Q098",
  "Q104",
  "Q114",
  "Q149",
]);

// These checks happened after the fixed 440-run snapshot. They are recorded
// separately and never overwrite the answer that was actually captured in the
// benchmark file.
const liveRechecks = {
  Q010: {
    verdict: "correct",
    note: "أعاد المستخدم السؤال حيًا بعد الاختبار وحصل على إجابة صحيحة؛ خطأ لقطة الاختبار متقطع وارتبط بتنسيق الرابط وفاحص الإجابة.",
  },
  Q139: {
    verdict: "correct",
    note: "أعاد المستخدم السؤال حيًا بعد الاختبار وحصل على إجابة جيدة؛ تبقى إجابة لقطة الاختبار محفوظة كما خرجت للمقارنة التاريخية.",
  },
};

// Manual Codex judgments. Any QID not listed here is accepted as correct under
// the user's intentionally lenient rubric: useful, responsive, and without a
// material error. This map does not contain or generate replacement answers.
const manual = {
  Q004: J("incorrect", 20, "صنّفت الجامعة بأنها حكومية، وهذا تصنيف جوهري غير صحيح؛ الإشراف أو الاعتماد الرسمي لا يجعل المؤسسة جامعة حكومية.", "generation_error"),
  Q006: J("incorrect", 20, "ساوت بين «جامعة غزة» والجامعة الإسلامية بغزة، مع أن الاسمين قد يشيران إلى مؤسستين مختلفتين؛ كان يجب عدم دمج الكيانين.", "entity_resolution"),
  Q010: J("incorrect", 35, "لم يقدّم الموقع الرسمي رغم أن نطاق الجامعة وموقعها الرسمي متاحان في الداتا، فبقي الطلب المباشر بلا إجابة.", "retrieval_failure"),
  Q016: J("partial", 68, "معظم قائمة الكليات والبرامج مفيد، لكن عناصر كلية الاقتصاد المذكورة هنا هي برامج دراسات عليا وليست قائمة بكالوريوس، مع وصف القائمة بأنها كاملة.", "coverage_incomplete"),
  Q017: J("partial", 68, "فصل مرحلة البكالوريوس كما طُلب، لكن خلط برامج دراسات عليا ضمن كلية الاقتصاد بقائمة البكالوريوس، لذلك القائمة ليست دقيقة بالكامل.", "coverage_incomplete"),
  Q019: J("incorrect", 25, "أدرج للفرع الأدبي تخصصات علمية وصحية وهندسية لا تسمح بها شروط القبول المعروضة، فالقائمة توسعت على نحو يغيّر قرار الطالب.", "constraint_reasoning"),
  Q024: J("incorrect", 15, "قال إن العلاج الطبيعي غير موجود، بينما أول مقطع مسترجع يذكره صراحة ضمن كلية العلوم الصحية؛ الخطأ وقع رغم وصول الدليل الصحيح للنموذج.", "generation_error"),
  Q031: J("partial", 62, "ميّز المنحة الكاملة عن المنح الجزئية وقدّم معلومات مفيدة، لكنه لم يحدد بوضوح التخصصات التي تشملها المنحة الكاملة كما طلب السؤال.", "coverage_incomplete"),
  Q035: J("partial", 72, "وجّه إلى الجهة الصحيحة وهي عمادة شؤون الطلبة، لكنه أرفق بريد القبول والتسجيل بدل بريد شؤون الطلبة، فهناك تعارض بين اسم الجهة ووسيلة الاتصال.", "source_precision"),
  Q038: J("partial", 55, "أجاب الجملة الأساسية بشكل صحيح: خريج الثانوية يتجه للبكالوريوس، ثم أضاف إجراءات إخلاء طرف وتخرج تخص طالبًا جامعيًا متخرجًا ولا تخص طالبًا مستجدًا.", "context_failure"),
  Q040: J("correct", 80, "وفق حكم المستخدم والمعيار المرن، التحفظ عند غياب رابط عام مع الإحالة إلى روابط الخطط والموقع الرسمي يحقق الغرض بدرجة مقبولة.", "none"),
  Q051: J("incorrect", 20, "استنتج أن حامل الثانوية الأردنية طالب وافد وغير فلسطيني؛ دولة الشهادة لا تحدد جنسية الطالب.", "generation_error"),
  Q055: J("incorrect", 38, "اكتفى بإحالة عامة ولم يذكر وسيلة التحقق الرسمية المتاحة مثل عمادة القبول والتسجيل وبريدها، مع أن هذا هو لب السؤال.", "retrieval_failure"),
  Q062: J("incorrect", 25, "جزم بأن المعادلة لا تحدد المعدل أو الفرع، بينما هذه الحالة غير محسومة في الداتا وقد تدخل الجهة المختصة في تصنيف الشهادة؛ اليقين هنا غير مبرر.", "data_gap_overclaim", true),
  Q063: J("incorrect", 25, "لم يشرح كيف تُعامل شهادة لا يظهر عليها الفرع، بل سرد تخصصات الفرع العلمي؛ لم يحل مشكلة تصنيف الشهادة التي سأل عنها المستخدم.", "context_failure"),
  Q064: J("incorrect", 10, "انتقل إلى قواعد الامتحان الإلكتروني وقرار غير مكتمل، وهي إجابة من سياق مختلف تمامًا عن التقديم قبل صدور المعادلة.", "context_failure"),
  Q069: J("incorrect", 10, "فسّر «الطلب» على أنه واجب في Moodle بدل طلب الالتحاق الجامعي، لذلك كل خطوات التحقق المذكورة تخص نظامًا آخر.", "context_failure"),
  Q070: J("partial", 60, "سمّى عمادة القبول والتسجيل بشكل صحيح، لكنه وجّه طالب الالتحاق إلى بوابة خدمات الطالب بدل بوابة/صفحة طلب الالتحاق المناسبة.", "context_failure"),
  Q074: J("partial", 70, "قدّم مسارًا مفيدًا للطالب الجديد ولم يحوّله إلى Moodle، لكنه ذكر رسم الطلب الاسمي دون توضيح الإعفاء الحالي وخلط بين بوابة الجدد ونموذج الطلب المباشر.", "coverage_incomplete"),
  Q080: J("incorrect", 20, "السياق كان يطلب خلاصة حدود القبول والدراسة عن بعد، لكن الرد تحول إلى قنوات الدفع والمساعدة المالية؛ لم يجب الخلاصة المطلوبة.", "context_failure"),
  Q083: J("partial", 58, "أعطى صيغ ملفات مناسبة وبعض الوثائق، لكنه لم يجب عن عدد الصور الشخصية، وهو نصف السؤال المباشر.", "coverage_incomplete", true),
  Q085: J("partial", 62, "فصل مرحلتي التقديم وما بعد القبول، لكن الجزم بعدم وجود مستندات إضافية غير موثق ثم جرى التراجع عنه بصياغة استثنائية.", "data_gap_overclaim", true),
  Q086: J("partial", 65, "اعتمد حالة موثقة حتى 16 يوليو للإجابة عن «اليوم» بعد ذلك، كما أن عبارة «لا يفتح حتى منتصف يوليو» أصبحت زمنياً غير منضبطة؛ الاتجاه مفيد لكن التحقق الآني غير كافٍ.", "temporal_verification", true),
  Q093: J("incorrect", 30, "امتنع عن ذكر معدل الطب رغم وجود بيانات حديثة في corpus تذكر 91% لعام 2025/2026 مع كونه تنافسيًا ومتغيرًا.", "retrieval_failure"),
  Q095: J("partial", 58, "ذكر حد الهندسة، لكنه أغفل حد الطب المتاح وربط الهندسة برابط لا يثبت الجدول المطلوب بوضوح؛ الجدول لم يكتمل.", "coverage_incomplete"),
  Q098: J("correct", 82, "وفق حكم المستخدم والمعيار المرن، قائمة البرامج التي يحقق معدل الطالب مفاتيحها مفيدة وكافية عمليًا؛ غياب عبارة نفي الضمان صراحةً لا يُعد خطأً ماديًا هنا.", "none"),
  Q099: J("incorrect", 20, "حصر خيارات معدل 75 في الشريعة والقانون فقط، مع أن الداتا تتضمن برامج كثيرة بحدود 65% و70% و75%.", "constraint_reasoning"),
  Q100: J("incorrect", 20, "احتوى تناقضات حسابية؛ اعتبر 65% كافيًا لكل البرامج العلمية ثم ذكر الهندسة 80%، كما وضع برامج تتطلب 80% ضمن فقرة 70%.", "constraint_reasoning"),
  Q103: J("incorrect", 30, "قال إن سعر ساعة الهندسة غير موجود، بينما الداتا الرسمية المسترجعة تذكر 28 دينارًا للساعة.", "retrieval_failure"),
  Q104: J("correct", 82, "وفق حكم المستخدم والمعيار المرن، ذكر رسم طلب الالتحاق والثوابت الفصلية المطلوبين مباشرةً؛ التفصيل الإضافي للفصل الأول ليس لازمًا لقبول الإجابة.", "none"),
  Q105: J("incorrect", 10, "أجاب عن رقم الجلوس والشهادة الخارجية بدل توجيه المستخدم إلى الدليل المالي عند غياب الرقم؛ هذا انتقال إلى نية مختلفة.", "context_failure"),
  Q108: J("incorrect", 20, "اخترع شرط أن تكون بطاقة الدفع باسم الطالب، وهو غير موجود في الداتا، وقد يمنع وسيلة دفع مشروعة دون أساس.", "data_gap_overclaim", true),
  Q114: J("correct", 80, "وفق حكم المستخدم والمعيار المرن، فرّق الرد بوضوح بين رسم طلب الالتحاق والثوابت الفصلية ونفى كونهما الرسم نفسه، وهذا كافٍ لقبول الإجابة.", "none"),
  Q115: J("incorrect", 15, "لم يلخّص أي رسم باسمه ومرحلته؛ اكتفى بذكر اسم مصدر وغياب تاريخ التحقق.", "generation_error"),
  Q121: J("incorrect", 15, "قال إن 85% لا يؤهل لأي منحة ثم ذكر بنفسه حدودًا أدنى مثل 80% و70% و65%؛ النتيجة تناقض الأرقام المعروضة.", "constraint_reasoning"),
  Q124: J("partial", 60, "الفكرة العامة بأن بعض المنح قد تتوقف صحيحة، لكن الرد استخدم شروط الثانوية الأولية كأنها شروط استمرار بعد الفصل الأول دون دليل.", "data_gap_overclaim", true),
  Q126: J("partial", 58, "أعطى إجراء تصديق منظمًا، لكنه أكد مساواة حامل الشهادة الخارجية في جميع منح المستجدين وفرض سلسلة تصديقات محددة دون نص يحسم ذلك.", "data_gap_overclaim", true),
  Q127: J("partial", 60, "أعطى جهة اتصال مفيدة، لكنه جزم بأن الإقامة خارج غزة لا تؤثر في المنحة مع عدم وجود قاعدة موثقة تحسم أهلية كل منحة.", "data_gap_overclaim", true),
  Q137: J("incorrect", 30, "لم يعط بريد عمادة الهندسة رغم وجود البريد الوظيفي deaneng@iugaza.edu.ps في الداتا والسياق.", "retrieval_failure"),
  Q139: J("incorrect", 20, "قدّم بريدين شخصيين لأكاديميين على أنهما بريدان وظيفيان ثابتان للعمادة؛ هذا يخلط نوع البريد واستمراريته.", "role_resolution"),
  Q141: J("partial", 55, "قدّم عناوين عامة للكليات وهي مفيدة للتواصل، لكنه لم يوضح أنه لا يملك بريد «كل أكاديمي» ولم ينفذ النطاق المطلوب.", "coverage_incomplete"),
  Q145: J("incorrect", 25, "طلب المستخدم بريد القسم العام عند غياب الدليل، لكن الرد أعطى بريد رئيس قسم شخصي ووصفه بأنه البريد العام.", "role_resolution"),
  Q149: J("correct", 78, "وفق حكم المستخدم والمعيار المرن، أعطى الرد نطاق الجامعة الرسمي كما طُلب؛ عدم الوصول إلى رابط الخطة المباشر عُدّ نقصًا غير مادي.", "none"),
  Q151: J("incorrect", 25, "فسّر اختلاف خطط طلاب التخصص نفسه باختلاف الكليات والتخصصات، وتجاهل السبب الأهم في السؤال: اختلاف دفعة القبول وإصدار الخطة.", "context_failure", true),
  Q152: J("incorrect", 20, "قال إن الخطة تتطابق لطالبين في التخصص والكلية نفسيهما، مع أن السؤال يفرق صراحة بين دفعة قديمة وجديدة وقد تنطبق عليهما إصدارات مختلفة.", "context_failure"),
  Q153: J("incorrect", 25, "حوّل سؤال تغيير المسار داخل التخصص إلى تحويل كلية/تخصص، وأضاف رسومًا ونسبة معادلة لا تجيب عن أثر المسار على الخطة.", "context_failure", true),
  Q154: J("incorrect", 10, "قدّم قاعدة معادلة غير منطقية تجعل مساق 3 ساعات يعادل 1.5 ساعة، وخلط تغيير اسم المساق بسياسة التحويل بين الجامعات.", "generation_error", true),
  Q166: J("partial", 58, "استخدم نطاق الساعات العام للبكالوريوس كأنه عدد مؤكد لهندسة الحاسوب، بينما الداتا المتاحة لا تعرض الرقم الخاص بالخطة نفسها.", "data_gap_overclaim", true),
  Q173: J("incorrect", 25, "نفى وجود امتحان تحديد مستوى إنجليزي بثقة، مع عدم وجود قاعدة رسمية في الداتا تثبت النفي.", "data_gap_overclaim", true),
  Q174: J("incorrect", 20, "بدأ بنفي قطعي لطلب TOEFL/IELTS ثم أقر في الجملة التالية بعدم وجود قاعدة موثقة؛ الجملتان متناقضتان.", "data_gap_overclaim", true),
  Q178: J("incorrect", 20, "طبّق شروط معادلة التحويل من جامعة أخرى على تحويل داخلي من العلوم إلى الهندسة، وهما إجراءان مختلفان.", "context_failure"),
  Q199: J("incorrect", 10, "ضمير «مدتها» يعود في السياق إلى المنحة، لكن الرد أجاب عن مدة دراسة الهندسة.", "context_failure"),
  Q203: J("partial", 60, "سمّى عمادة شؤون الطلبة وذكر أن التوقيت يختلف، لكنه لم يجب مباشرة هل المنحة تلقائية أم تحتاج طلبًا.", "answer_focus", true),
  Q205: J("partial", 55, "أعطى صفحة رسمية مفيدة، لكنه لم يعط رقم اتصال رسمي رغم أن هذا هو الطلب الصريح وكانت أرقام التواصل متاحة.", "answer_focus"),
  Q210: J("incorrect", 25, "لم يقدّم البدائل الآمنة للطلبات السابقة واحدًا واحدًا، واكتفى برسالة عامة لا تستفيد من السياق.", "context_failure"),
  Q212: J("incorrect", 30, "لم يحدد الفقرة أو اسم الدليل المقصود ولم يشرح حدود المصدر، مع أن السؤال يطلب تتبع الدليل لا معلومة جديدة.", "source_precision"),
  Q214: J("incorrect", 20, "نسب كل المعلومات السابقة إلى تاريخ واحد 2026-07-15، بينما المقاطع تحمل تواريخ تحقق متعددة بينها 16 و18 يوليو.", "metadata_error"),
  Q215: J("partial", 55, "لخص بعض الحقائق الصحيحة وذكر جهة التحقق، لكنه ادعى غياب بقية بيانات القبول وأدخل رسم تصديق خريج غير مرتبط بالسياق.", "coverage_incomplete"),
  Q217: J("partial", 68, "قائمة تخصصات الحاسوب مفيدة في معظمها، لكن ادعاء أن منحة غير الناطقين بالعربية تشمل هذه التخصصات تحديدًا غير موثق.", "data_gap_overclaim", true),
  Q220: J("incorrect", 30, "لم يختم بأي رابط أو جهة اتصال ولم يحدد النقاط المتغيرة، رغم توافر هذه البيانات في السياق.", "generation_error"),
  Q238: J("partial", 58, "بدأ بـ«نعم» بما قد يوحي بكتابة الهوية مكان رقم الجلوس، ثم قال وضعها في خانة الهوية؛ التوجيه متناقض في موضع الحقل.", "context_failure"),
  Q244: J("partial", 65, "ذكر منح الأقسام واستثناءات مفيدة، لكنه عمم أن بقية المنح متاحة لكل تخصصات البكالوريوس دون دليل يحسم نطاق كل منحة.", "data_gap_overclaim", true),
  Q246: J("partial", 62, "قدّم قائمة واسعة وصحيحة في معظمها لمعدل 85 علمي، لكنه وضع التمريض ضمن البرامج غير الممكنة رغم أن حده 70% للعلمي.", "constraint_reasoning"),
  Q247: J("incorrect", 20, "حصر المنح في منحة الامتياز واستنتج عدم وجود أي منحة عند 85%، متجاهلًا منح الفئات والأقسام والمنح ذات شروط ثانوية أقل.", "constraint_reasoning"),
  Q248: J("incorrect", 15, "اختار منحة ذوي الأسرى من دون مرجع في السؤال أو السياق القريب، فشرح شروط منحة لم يحددها المستخدم.", "context_failure"),
  Q251: J("incorrect", 15, "تعامل مع «جامعة غزة» على أنها الجامعة الإسلامية ثم ذكر كليات غير صحيحة مثل التربية الفنية والرياضية وحذف كليات أساسية.", "entity_resolution"),
  Q252: J("incorrect", 15, "بعد توضيح أن المقصود الجامعة الإسلامية، استمرت قائمة الكليات الخاطئة وحذفت الطب وتكنولوجيا المعلومات والعلوم الصحية.", "generation_error"),
  Q255: J("incorrect", 10, "السياق يسأل عن شروط البرنامج، لكن الرد تحدث عن أسعار ورسوم و«شروط استخدام» وأضاف رسومًا غير موثقة.", "context_failure"),
  Q256: J("incorrect", 20, "نفى وجود منح للمتفوقين خارج الهندسة، بينما الداتا تتضمن منح الامتياز والوزارة ولا تربطها بالهندسة فقط.", "retrieval_failure"),
  Q267: J("partial", 58, "ذكر اختلافات عامة بين الكليات والمسارات والاختياريات، لكنه لم يلتقط سبب اختلاف خطة طالبين في التخصص نفسه: دفعة القبول وإصدار الخطة.", "context_failure"),
  Q274: J("incorrect", 20, "وصف 19 سبتمبر 2026 بأنه تقدير غير موثق رغم أن التقويم الأكاديمي المسترجع يورده موعدًا رسميًا.", "generation_error"),
  Q275: J("partial", 65, "فصل بين قبول الدراسات العليا والبكالوريوس، لكن سؤال «اليوم» يحتاج تحققًا آنيًا بينما الداتا المستخدمة كانت أقدم بعدة أيام.", "temporal_verification", true),
  Q276: J("partial", 58, "السياق عن قبول البكالوريوس، لكن الرد بدأ بمواعيد الدراسات العليا واكتفى بتوقع عام للبكالوريوس دون موعد فتح مؤكد.", "context_failure", true),
  Q277: J("incorrect", 30, "لم يعط صفحة الإعلانات الرسمية رغم وجود الرابط admission.iugaza.edu.ps/e3lan في الداتا.", "retrieval_failure"),
  Q283: J("partial", 58, "أجاب بوضوح أنه لا يلزم الطبع، لكن لا توجد في الداتا قاعدة موثقة تثبت هذا الجزم.", "data_gap_overclaim", true),
  Q293: J("partial", 60, "اعترف بعدم وجود تفاصيل مختبرات محددة ووجّه للكلية، لكن عنوان الاتصال مكتوب بصيغة نطاق بلا علامة @ ولا يحقق تواصلًا صالحًا.", "source_precision", true),
  Q294: J("partial", 55, "عرض مراكز وتخصصات ورسومًا عامة ثم أقر بعدم توفر أسماء مختبرات هندسة الحاسوب؛ لم ينفذ «اذكرهم» وفق مرجع السؤال.", "context_failure", true),
  Q296: J("incorrect", 20, "جزم بعدم وجود سكن جامعي رغم أن الداتا لا تحسم وجوده أو عدمه، وأضاف رقم اتصال غير متسق.", "data_gap_overclaim", true),
  Q297: J("incorrect", 25, "بنى البدائل على نفي غير موثق لوجود السكن، ثم نسب للجامعة تنسيق خيارات سكن دون دليل واضح.", "data_gap_overclaim", true),
  Q303: J("incorrect", 15, "نسب فرع الجنوب إلى صالة عابدين في النصيرات، بينما الداتا المتاحة لا تثبت هذا الموقع وتذكر أن تسجيل الفرع معطل حاليًا؛ يبدو أنه خلطه بموقع استقبال مؤقت.", "context_failure", true),
  Q304: J("incorrect", 5, "قال إن فرع الجنوب هو فرع «خارج القطاع» ونصح طالبًا خارج غزة باختياره، مع أن الفرع داخل قطاع غزة والتسجيل فيه معطل.", "context_failure"),
  Q306: J("incorrect", 35, "لم يعط البريد الرسمي العام رغم توفر بريد القبول والتسجيل والبريد العام للكليات في الداتا.", "retrieval_failure"),
  Q308: J("partial", 55, "قدّم عناوين عامة مفيدة لمعظم الكليات بدل بريد كل عميد، ووضع بريد الهندسة لكلية الطب؛ لذلك القائمة لا تحقق الطلب بدقة.", "role_resolution"),
  Q309: J("incorrect", 35, "امتنع عن إعطاء البريد الجامعي المنشور رسميًا رغم توفر العناوين العامة ومصادرها في الداتا.", "retrieval_failure"),
  Q337: J("incorrect", 30, "لم يشرح أي معيار للتحقق من رابط الدفع، مثل النطاق الرسمي أو الدخول من البوابة الرسمية، مع أن السؤال أمني ومباشر.", "safety_guidance"),
  Q340: J("incorrect", 10, "المستخدم طالب ثانوية لم يسجل بعد، لكن الرد أعطاه رسوم ماجستير ودبلوم وإجراءات غير متسقة مع البكالوريوس.", "context_failure"),
  Q362: J("partial", 58, "القائمة والرسوم التاريخية لفرع الجنوب مفيدة، لكنها صيغت كأن البرامج متاحة الآن ولم تذكر أن التسجيل والتخفيضات معطلان حاليًا.", "temporal_verification"),
  Q364: J("incorrect", 15, "خلط الثوابت الفصلية بسعر الساعة وقال إنها 25 دينارًا لكل ساعة؛ الثابت الفصلي ليس رسمًا ساعيًا.", "generation_error"),
  Q372: J("incorrect", 10, "السياق يتحدث عن معادلة شهادة ثانوية خارجية، لكن الرد شرح معادلة مساقات عند التحويل من جامعة أخرى.", "context_failure"),
  Q375: J("partial", 55, "الرسالة غير مكتملة وكان الأنسب طلب توضيح، لكن الرد افترض أن المقصود رسوم طلبات الالتحاق وسردها كلها.", "clarification_failure"),
  Q388: J("partial", 68, "قدّم مقارنة مفيدة وتوصية معقولة لمحبي البرمجة، لكنه وصف هندسة الذكاء الاصطناعي كمسار داخل هندسة الحاسوب بينما هي برنامج مستقل في الداتا.", "generation_error"),
  Q390: J("partial", 55, "ذكر رسومًا ورابط خطة وجهة اتصال مفيدة، لكن شروط القبول التفصيلية تبدو منقولة من برنامج تقني آخر وليست موثقة لماجستير هندسة الحاسوب نفسه.", "retrieval_contamination"),
  Q391: J("partial", 62, "استخدم نطاق البكالوريوس العام 128–136 للهندسة كلها ولم يذكر استثناء العمارة 152 ساعة، لذلك الإجابة العامة ليست دقيقة لكل برامج الهندسة.", "data_gap_overclaim", true),
  Q392: J("partial", 55, "أكد أن هندسة الحاسوب 128–136 ساعة اعتمادًا على نطاق عام، بينما الرقم الخاص بخطة هندسة الحاسوب غير ظاهر في الداتا المسترجعة.", "data_gap_overclaim", true),
  Q407: J("partial", 60, "حاول فصل الامتحانات عن الوثائق وذكر عدم ضمان البديل، لكنه جمع بين نفي البديل وإثبات خيار إلكتروني ثم جزم بعدم الحاجة للورق دون سياسة حديثة واضحة.", "data_gap_overclaim", true),
  Q409: J("partial", 65, "شرح المراحل الأساسية بصورة مفيدة، لكن تعريف تثبيت المقعد وإعطائه «رقم جلوس ثابت» غير موثق ويخلط رقم الجلوس بالرقم الجامعي.", "generation_error"),
  Q410: J("incorrect", 20, "افترض وجود رقم مستقل لطلب الالتحاق، بينما مسار الداتا يبدأ بالحصول على الرقم الجامعي ثم استخدامه لتعبئة الطلب، ولا يثبت رقمًا ثانيًا بهذه الصفة.", "generation_error"),
  Q412: J("incorrect", 25, "لم يشرح أن وجود كلمة iug داخل الرابط لا يكفي وأن التحقق يكون من النطاق الرسمي iugaza.edu.ps وبنية الرابط؛ ترك سؤال أمان الرابط بلا جواب.", "safety_guidance"),
  Q413: J("partial", 70, "النصيحة الأمنية الأساسية صحيحة: لا ترفع وثائق حساسة في نموذج غير موثوق، لكن القول إن الجامعة لا تطلب الهوية أو الجواز إطلاقًا في القبول غير صحيح.", "safety_overclaim"),
  Q414: J("incorrect", 20, "فسّر «هنا» على أنه نظام الجامعة وابتكر مسار حذف عبر العمادة، بدل توضيح حدود حذف بيانات المحادثة أو جهة المنصة الحالية.", "context_failure", true),
  Q420: J("incorrect", 15, "اعتبر الفصل القادم هو الفصل الثاني في فبراير 2027، بينما السياق والتاريخ الحالي يجعلان الفصل الأول 2026/2027 في سبتمبر 2026 هو القادم.", "context_failure"),
  Q423: J("incorrect", 25, "قال إن رسوم الساعة غير موجودة، رغم وجود جدول رسوم البكالوريوس في الداتا؛ كما لم يذكر تاريخ تحقق الجدول المتاح.", "retrieval_failure"),
  Q424: J("incorrect", 20, "قال إن منحة 2025/2026 هي المنحة المفتوحة الآن في يوليو 2026، مع أن الداتا نفسها لا تحدد المنح المفتوحة لحظيًا.", "temporal_verification", true),
  Q426: J("incorrect", 25, "لم يعط رابط خطة هندسة الحاسوب رغم وجوده في بيانات البرنامج، مع أن المستخدم سمح صراحة بذكر أن سنة الخطة غير معروفة.", "retrieval_failure"),
  Q427: J("partial", 58, "القول إن المستجد يتبع الخطة الجديدة منطقي غالبًا، لكنه جزم به دون وجود قاعدة دفعات أو رقم إصدار في الداتا.", "data_gap_overclaim", true),
  Q438: J("partial", 58, "أعطى برامج كلية تكنولوجيا المعلومات المناسبة لمعدل 85 علمي، لكنه قال إن الهندسة لا تحتوي برنامج حاسوب وأغفل هندسة الحاسوب وهندسة الذكاء الاصطناعي.", "coverage_incomplete"),
  Q440: J("incorrect", 5, "ضمير «اذكرهم» يعود إلى المنح المذكورة في السؤال السابق، لكن الرد سرد كليات الجامعة؛ فقد سياق المرجع بالكامل.", "context_failure"),
};

const B = (verdict, score = verdict === "correct" ? 90 : verdict === "partial" ? 65 : 30) => ({
  verdict,
  score,
  reason: verdict === "correct"
    ? "الإجابة السابقة كانت مقبولة وفق المعيار المرن ولم يظهر فيها خطأ مادي."
    : verdict === "partial"
      ? "الإجابة السابقة احتوت جزءًا مفيدًا، لكن بقي فيها نقص أو ادعاء غير محسوم."
      : "الإجابة السابقة كانت خاطئة جوهريًا أو لم تجب عن السياق المطلوب.",
});

// Same-rubric review for every case that looked like a regression when compared
// only with the historical stored label. This prevents an old labeling mistake
// (or a previously bad but nearly identical answer) from being counted as a
// regression caused by the new pipeline.
const manualBefore = {
  Q010: B("correct"),
  Q024: B("incorrect"),
  Q031: B("incorrect"),
  Q035: B("partial"),
  Q038: B("correct"),
  Q040: B("partial"),
  Q051: B("partial"),
  Q055: B("correct"),
  Q062: B("incorrect"),
  Q063: B("incorrect"),
  Q064: B("partial"),
  Q069: B("partial"),
  Q074: B("correct"),
  Q085: B("partial"),
  Q086: B("correct"),
  Q104: B("partial"),
  Q124: B("partial"),
  Q126: B("correct"),
  Q127: B("partial"),
  Q137: B("correct"),
  Q141: B("partial"),
  Q152: B("correct"),
  Q166: B("partial"),
  Q173: B("correct"),
  Q174: B("correct"),
  Q199: B("correct"),
  Q203: B("incorrect"),
  Q212: B("partial"),
  Q214: B("partial"),
  Q217: B("correct"),
  Q220: B("correct"),
  Q238: B("correct"),
  Q244: B("partial"),
  Q246: B("correct"),
  Q247: B("partial"),
  Q252: B("partial"),
  Q267: B("partial"),
  Q274: B("correct"),
  Q276: B("partial"),
  Q277: B("correct"),
  Q283: B("partial"),
  Q293: B("partial"),
  Q296: B("correct"),
  Q297: B("correct"),
  Q304: B("correct"),
  Q306: B("correct"),
  Q308: B("partial"),
  Q337: B("correct"),
  Q340: B("partial"),
  Q372: B("correct"),
  Q375: B("partial"),
  Q388: B("correct"),
  Q390: B("partial"),
  Q391: B("partial"),
  Q392: B("partial"),
  Q407: B("incorrect"),
  Q409: B("partial"),
  Q410: B("incorrect"),
  Q412: B("correct"),
  Q414: B("incorrect"),
  Q420: B("correct"),
  Q424: B("incorrect"),
  Q426: B("correct"),
  Q427: B("partial"),
  Q438: B("partial"),
  Q440: B("partial"),
};

const verdictAr = {
  correct: "صحيحة/مقبولة",
  partial: "مقبولة جزئيًا",
  incorrect: "خاطئة",
};

const rootCauseAr = {
  none: "لا توجد مشكلة مادية",
  answer_focus: "عدم الإجابة المباشرة عن محور السؤال",
  clarification_failure: "عدم طلب التوضيح عند غموض السؤال",
  constraint_reasoning: "خطأ في تطبيق القيود أو الحدود",
  context_failure: "فقدان سياق المحادثة أو مرجع الضمير",
  coverage_incomplete: "تغطية ناقصة أو قائمة غير مكتملة",
  data_gap_overclaim: "جزم بمعلومة غير محسومة في الداتا",
  entity_resolution: "خلط بين كيانين أو اسمين",
  generation_error: "خطأ توليد رغم توفر المعلومة أو القاعدة",
  metadata_error: "خطأ في تاريخ أو metadata المصدر",
  retrieval_contamination: "خلط أدلة من برنامج أو موضوع آخر",
  retrieval_failure: "عدم استخدام معلومة موجودة في corpus",
  role_resolution: "خلط بين بريد شخصي ووظيفي أو بين الجهات",
  safety_guidance: "إرشاد أمان غير كافٍ",
  safety_overclaim: "نصيحة أمان صحيحة مع تعميم خاطئ",
  source_precision: "مصدر أو رابط أو جهة اتصال غير دقيقة",
  temporal_verification: "معلومة زمنية لم تُتحقق لحظة السؤال",
};

function mean(values) {
  const clean = values.filter(Number.isFinite);
  return clean.length ? clean.reduce((a, b) => a + b, 0) / clean.length : null;
}

function percentile(values, p) {
  const clean = values.filter(Number.isFinite).sort((a, b) => a - b);
  if (!clean.length) return null;
  return clean[Math.max(0, Math.ceil(clean.length * p) - 1)];
}

function round(value, digits = 2) {
  if (!Number.isFinite(value)) return null;
  const factor = 10 ** digits;
  return Math.round(value * factor) / factor;
}

function normalizeText(value) {
  return String(value ?? "")
    .toLowerCase()
    .replace(/https?:\/\/\S+/g, " ")
    .replace(/[^\p{L}\p{N}]+/gu, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function trigramDice(a, b) {
  const left = `  ${normalizeText(a)}  `;
  const right = `  ${normalizeText(b)}  `;
  if (left === right) return 1;
  const grams = (text) => {
    const map = new Map();
    for (let i = 0; i < text.length - 2; i += 1) {
      const gram = text.slice(i, i + 3);
      map.set(gram, (map.get(gram) ?? 0) + 1);
    }
    return map;
  };
  const g1 = grams(left);
  const g2 = grams(right);
  let overlap = 0;
  for (const [gram, count] of g1) {
    overlap += Math.min(count, g2.get(gram) ?? 0);
  }
  const n1 = [...g1.values()].reduce((a0, b0) => a0 + b0, 0);
  const n2 = [...g2.values()].reduce((a0, b0) => a0 + b0, 0);
  return n1 + n2 ? (2 * overlap) / (n1 + n2) : 0;
}

function comparisonAgainstRecorded(previousVerdict, currentVerdict, similarity) {
  if (similarity >= 0.82) {
    return {
      code: "near_unchanged",
      ar: "الإجابة لم تتغير جوهريًا؛ اختلاف الحكم عن التصنيف القديم يعني اختلافًا في دقة التحكيم لا تراجعًا سلوكيًا مؤكّدًا.",
    };
  }
  if (previousVerdict === "incorrect" && currentVerdict === "correct") {
    return { code: "improved_vs_recorded", ar: "تحسن مقابل التصنيف السابق المسجّل." };
  }
  if (previousVerdict === "incorrect" && currentVerdict === "partial") {
    return { code: "partly_improved_vs_recorded", ar: "تحسن جزئي مقابل التصنيف السابق، لكن بقيت ملاحظة مؤثرة." };
  }
  if (previousVerdict === "incorrect" && currentVerdict === "incorrect") {
    return { code: "still_problematic", ar: "بقيت المشكلة دون حل كافٍ مقابل التصنيف السابق." };
  }
  if (previousVerdict === "correct" && currentVerdict === "correct") {
    return { code: "maintained", ar: "حافظت النسخة الجديدة على إجابة مقبولة." };
  }
  if (previousVerdict === "correct" && currentVerdict === "partial") {
    return { code: "apparent_partial_regression", ar: "تراجع ظاهري من صحيح مسجّل سابقًا إلى إجابة جزئية في التحكيم الحالي." };
  }
  if (previousVerdict === "correct" && currentVerdict === "incorrect") {
    return { code: "apparent_regression", ar: "تراجع ظاهري من صحيح مسجّل سابقًا إلى خطأ في التحكيم الحالي." };
  }
  return { code: "unclassified", ar: "لا توجد مقارنة قابلة للتصنيف." };
}

function comparisonSameRubric(previousVerdict, currentVerdict) {
  if (previousVerdict === "correct" && currentVerdict === "correct") {
    return { code: "maintained_correct", ar: "حافظت النسخة الجديدة على إجابة صحيحة/مقبولة." };
  }
  if (previousVerdict === "correct" && currentVerdict === "partial") {
    return { code: "regressed_to_partial", ar: "تراجعت من صحيحة/مقبولة إلى مقبولة جزئيًا." };
  }
  if (previousVerdict === "correct" && currentVerdict === "incorrect") {
    return { code: "regressed_to_incorrect", ar: "تراجعت من صحيحة/مقبولة إلى خاطئة." };
  }
  if (previousVerdict === "partial" && currentVerdict === "correct") {
    return { code: "improved_to_correct", ar: "تحسنت من مقبولة جزئيًا إلى صحيحة/مقبولة." };
  }
  if (previousVerdict === "partial" && currentVerdict === "partial") {
    return { code: "maintained_partial", ar: "بقيت مقبولة جزئيًا دون حل كامل." };
  }
  if (previousVerdict === "partial" && currentVerdict === "incorrect") {
    return { code: "regressed_partial_to_incorrect", ar: "تراجعت من مقبولة جزئيًا إلى خاطئة." };
  }
  if (previousVerdict === "incorrect" && currentVerdict === "correct") {
    return { code: "improved_to_correct", ar: "تحسنت من خاطئة إلى صحيحة/مقبولة." };
  }
  if (previousVerdict === "incorrect" && currentVerdict === "partial") {
    return { code: "improved_to_partial", ar: "تحسنت من خاطئة إلى مقبولة جزئيًا." };
  }
  if (previousVerdict === "incorrect" && currentVerdict === "incorrect") {
    return { code: "maintained_incorrect", ar: "بقيت خاطئة دون حل كافٍ." };
  }
  return { code: "unclassified", ar: "لا توجد مقارنة قابلة للتصنيف." };
}

function csvCell(value) {
  const text = Array.isArray(value)
    ? value.join(" | ")
    : typeof value === "object" && value !== null
      ? JSON.stringify(value)
      : String(value ?? "");
  return `"${text.replaceAll('"', '""')}"`;
}

function unique(values) {
  return [...new Set(values.filter((value) => value !== null && value !== undefined && value !== ""))];
}

function markdownText(value) {
  return String(value ?? "").replaceAll("\r\n", "\n").trim();
}

function renderAnswer(value) {
  const text = markdownText(value);
  return text ? text.split("\n").map((line) => `> ${line}`).join("\n") : "> —";
}

if (!fs.existsSync(inputPath)) {
  throw new Error(`Missing input: ${inputPath}`);
}

const rows = fs
  .readFileSync(inputPath, "utf8")
  .split(/\r?\n/)
  .filter(Boolean)
  .map((line, index) => {
    try {
      return JSON.parse(line);
    } catch (error) {
      throw new Error(`Invalid JSONL at line ${index + 1}: ${error.message}`);
    }
  });

if (rows.length !== 440) {
  throw new Error(`Expected 440 records, got ${rows.length}`);
}

const rowIds = new Set(rows.map((row) => row.qid));
const unknownManualIds = Object.keys(manual).filter((qid) => !rowIds.has(qid));
if (unknownManualIds.length) {
  throw new Error(`Manual judgments contain unknown QIDs: ${unknownManualIds.join(", ")}`);
}

const evaluations = rows.map((row) => {
  const judgment = manual[row.qid] ?? J(
    "correct",
    90,
    "الإجابة تجيب عن المطلوب بدرجة كافية، ولا يظهر فيها خطأ مادي يؤثر في قرار المستخدم وفق الداتا المتاحة وسياق السؤال.",
    "none",
    false,
  );
  const similarity = trigramDice(row.before_answer, row.after_answer);
  const beforeJudgment = manualBefore[row.qid]
    ?? (similarity >= 0.82
      ? B(judgment.verdict, judgment.score)
      : B(row.before_verdict === "correct" ? "correct" : "incorrect"));
  const cmp = comparisonSameRubric(beforeJudgment.verdict, judgment.verdict);
  const recordedCmp = comparisonAgainstRecorded(row.before_verdict, judgment.verdict, similarity);
  const retrieval = row.retrieval_metadata ?? {};
  const retrievalTrace = (row.rag_trace ?? []).find((item) => item.scope === "uploaded_files_all") ?? {};
  const retrievalPlan = (row.rag_trace ?? []).find((item) => item.scope === "retrieval_plan") ?? {};
  const rerankerTrace = (row.rag_trace ?? []).find((item) => item.scope === "reranker") ?? {};
  const topSources = unique(row.top_chunk_sources ?? []);
  const selectedSources = unique(
    (retrievalTrace.candidates ?? [])
      .filter((candidate) => candidate.selected_by_ranker)
      .map((candidate) => candidate.file),
  );
  const latency = row.latency_breakdown ?? {};

  return {
    qid: row.qid,
    turn: row.turn,
    scenario_id: row.scenario_id,
    scenario_title: row.scenario_title,
    difficulty: row.difficulty,
    question: row.question,
    before_answer: row.before_answer,
    after_answer: row.after_answer,
    previous_recorded_verdict: row.before_verdict,
    previous_recorded_findings: row.before_findings,
    previous_codex_verdict: beforeJudgment.verdict,
    previous_codex_verdict_ar: verdictAr[beforeJudgment.verdict],
    previous_codex_score: beforeJudgment.score,
    previous_codex_reason_ar: beforeJudgment.reason,
    previous_codex_evaluation_basis: manualBefore[row.qid]
      ? "manual_same_rubric_review"
      : similarity >= 0.82
        ? "near_unchanged_inherits_current_judgment"
        : "historical_label_retained_after_consistency_check",
    current_verdict: judgment.verdict,
    current_verdict_ar: verdictAr[judgment.verdict],
    score: judgment.score,
    reason_ar: judgment.reason,
    judgment_basis: userAcceptedCorrect.has(row.qid)
      ? "user_lenient_override"
      : "codex_manual_review",
    live_recheck: liveRechecks[row.qid] ?? null,
    root_cause: judgment.rootCause,
    root_cause_ar: rootCauseAr[judgment.rootCause] ?? judgment.rootCause,
    needs_data_addition: judgment.needsDataAddition,
    comparison_code: cmp.code,
    comparison_ar: cmp.ar,
    comparison_vs_recorded_code: recordedCmp.code,
    comparison_vs_recorded_ar: recordedCmp.ar,
    before_after_similarity: round(similarity, 4),
    answer_changed_materially: similarity < 0.82,
    metadata: {
      before_source: row.before_source,
      after_source: row.after_source,
      before_latency_ms: row.before_latency_ms,
      after_latency_ms: row.after_latency_ms,
      latency_breakdown: {
        total_ms: latency.total_ms ?? row.after_latency_ms,
        openrouter_generation_ms: latency.openrouter_generation_ms ?? null,
        jina_embeddings_ms: latency.jina_embeddings_ms ?? null,
        jina_reranker_ms: latency.jina_reranker_ms ?? null,
        local_pipeline_ms_estimate: latency.local_pipeline_ms_estimate ?? null,
      },
      top_chunk_count: row.top_chunk_count,
      top_chunk_sources: topSources,
      selected_retrieval_sources: selectedSources,
      search_query: retrieval.search_query ?? retrievalPlan.search_query ?? null,
      base_query: retrieval.base_query ?? retrievalPlan.base_query ?? null,
      retrieval_strategy: retrievalTrace.strategy ?? null,
      candidate_count: retrievalTrace.candidate_count ?? null,
      allowed_collection_count: retrievalTrace.allowed_collection_count ?? null,
      rerank_requested: retrieval.rerank_requested ?? retrievalPlan.rerank_requested ?? false,
      rerank_attempted: retrieval.rerank_attempted ?? retrievalPlan.rerank_attempted ?? false,
      rerank_status: retrieval.rerank_status ?? retrievalPlan.rerank_status ?? rerankerTrace.status ?? "not_requested",
      reranker_model: rerankerTrace.model ?? null,
      reranker_latency_ms: rerankerTrace.latency_ms ?? latency.jina_reranker_ms ?? null,
      cache_hit: retrieval.cache_hit ?? false,
      answer_check_retry: retrieval.answer_check_retry ?? false,
      answer_check_issues: retrieval.answer_check_issues ?? [],
      answer_check_post_retry_issues: retrieval.answer_check_post_retry_issues ?? [],
      llm_call_count: row.llm_call_count,
      generation_model: row.benchmark?.model ?? null,
      technical_error: row.error ?? null,
      started_at: row.started_at,
      finished_at: row.finished_at,
      raw_result_file: path.relative(scriptDir, inputPath).replaceAll("\\", "/"),
    },
  };
});

const countBy = (items, selector) => {
  const result = {};
  for (const item of items) {
    const key = selector(item);
    result[key] = (result[key] ?? 0) + 1;
  }
  return Object.fromEntries(Object.entries(result).sort(([a], [b]) => a.localeCompare(b)));
};

const verdictCounts = countBy(evaluations, (item) => item.current_verdict);
const previousCounts = countBy(evaluations, (item) => item.previous_recorded_verdict);
const previousCodexCounts = countBy(evaluations, (item) => item.previous_codex_verdict);
const comparisonCounts = countBy(evaluations, (item) => item.comparison_code);
const recordedComparisonCounts = countBy(evaluations, (item) => item.comparison_vs_recorded_code);
const rootCauseCounts = countBy(
  evaluations.filter((item) => item.current_verdict !== "correct"),
  (item) => item.root_cause,
);
const rerankCounts = countBy(evaluations, (item) => item.metadata.rerank_status);
const nonCorrect = evaluations.filter((item) => item.current_verdict !== "correct");
const incorrect = evaluations.filter((item) => item.current_verdict === "incorrect");
const partial = evaluations.filter((item) => item.current_verdict === "partial");
const dataGapIssues = nonCorrect.filter((item) => item.needs_data_addition);
const nonDataIssues = nonCorrect.filter((item) => !item.needs_data_addition);
const retrievalIssues = nonCorrect.filter((item) =>
  ["retrieval_failure", "retrieval_contamination", "source_precision", "coverage_incomplete"].includes(item.root_cause),
);
const rankerRelevant = retrievalIssues.filter((item) => item.metadata.rerank_status !== "applied");
const regressions = evaluations.filter((item) =>
  ["regressed_to_partial", "regressed_to_incorrect", "regressed_partial_to_incorrect"].includes(item.comparison_code),
);
const improvements = evaluations.filter((item) =>
  ["improved_to_correct", "improved_to_partial"].includes(item.comparison_code),
);
const correctToIncorrect = evaluations.filter((item) => item.comparison_code === "regressed_to_incorrect");
const correctToPartial = evaluations.filter((item) => item.comparison_code === "regressed_to_partial");
const partialToIncorrect = evaluations.filter((item) => item.comparison_code === "regressed_partial_to_incorrect");
const apparentRegressionsVsRecorded = evaluations.filter((item) =>
  ["apparent_regression", "apparent_partial_regression"].includes(item.comparison_vs_recorded_code),
);
const nearUnchanged = evaluations.filter((item) => item.comparison_vs_recorded_code === "near_unchanged");

const beforeLatencies = evaluations.map((item) => item.metadata.before_latency_ms).filter(Number.isFinite);
const afterLatencies = evaluations.map((item) => item.metadata.after_latency_ms).filter(Number.isFinite);
const beforeMean = mean(beforeLatencies);
const afterMean = mean(afterLatencies);

const summary = {
  report_version: "codex-manual-lenient-v2-user-adjusted",
  generated_at: new Date().toISOString(),
  evaluation_date: "2026-07-19",
  evaluator: "Codex (manual local review; no external evaluator API)",
  total_questions: evaluations.length,
  rubric: {
    correct: "أجاب المطلوب بدرجة كافية بلا خطأ مادي؛ لا يشترط الكمال.",
    partial: "جزء مفيد مع نقص رئيسي أو ادعاء غير موثق يمكن أن يؤثر في الاستخدام.",
    incorrect: "معلومة جوهرية خاطئة، أو فقدان للسياق، أو عدم إجابة عن الطلب الأساسي.",
  },
  current_verdict_counts: verdictCounts,
  current_correct_rate_pct: round((verdictCounts.correct ?? 0) * 100 / evaluations.length),
  current_usable_rate_pct: round(((verdictCounts.correct ?? 0) + (verdictCounts.partial ?? 0)) * 100 / evaluations.length),
  previous_recorded_verdict_counts: previousCounts,
  previous_codex_normalized_verdict_counts: previousCodexCounts,
  comparison_counts: comparisonCounts,
  comparison_vs_historical_recorded_label_counts: recordedComparisonCounts,
  regression_counts_same_rubric: {
    total: regressions.length,
    correct_to_incorrect: correctToIncorrect.length,
    correct_to_partial: correctToPartial.length,
    partial_to_incorrect: partialToIncorrect.length,
  },
  root_cause_counts_non_correct: rootCauseCounts,
  issues_requiring_data_addition: dataGapIssues.length,
  issues_not_requiring_data_addition: nonDataIssues.length,
  cache_hits: evaluations.filter((item) => item.metadata.cache_hit).length,
  rerank_status_counts: rerankCounts,
  retrieval_related_non_correct_count: retrievalIssues.length,
  ranker_relevant_without_successful_application_count: rankerRelevant.length,
  technical_errors: evaluations.filter((item) => item.metadata.technical_error).length,
  latency_ms: {
    before: {
      mean: round(beforeMean),
      median: round(percentile(beforeLatencies, 0.5)),
      p95: round(percentile(beforeLatencies, 0.95)),
      min: Math.min(...beforeLatencies),
      max: Math.max(...beforeLatencies),
    },
    after: {
      mean: round(afterMean),
      median: round(percentile(afterLatencies, 0.5)),
      p95: round(percentile(afterLatencies, 0.95)),
      min: Math.min(...afterLatencies),
      max: Math.max(...afterLatencies),
    },
    mean_change_pct: round((afterMean - beforeMean) * 100 / beforeMean),
  },
  qid_lists: {
    incorrect: incorrect.map((item) => item.qid),
    partial: partial.map((item) => item.qid),
    data_addition_needed: dataGapIssues.map((item) => item.qid),
    non_data_issues: nonDataIssues.map((item) => item.qid),
    regressions_same_rubric: regressions.map((item) => item.qid),
    regressions_correct_to_incorrect: correctToIncorrect.map((item) => item.qid),
    regressions_correct_to_partial: correctToPartial.map((item) => item.qid),
    regressions_partial_to_incorrect: partialToIncorrect.map((item) => item.qid),
    improvements_same_rubric: improvements.map((item) => item.qid),
    apparent_regressions_vs_historical_recorded_label: apparentRegressionsVsRecorded.map((item) => item.qid),
    near_unchanged_answers: nearUnchanged.map((item) => item.qid),
    ranker_relevant_without_successful_application: rankerRelevant.map((item) => item.qid),
    user_accepted_as_correct: [...userAcceptedCorrect],
    correct_on_live_recheck_after_snapshot: Object.keys(liveRechecks),
  },
  live_rechecks_after_snapshot: liveRechecks,
  notes: [
    "لم يُستخدم مفتاح API أو نموذج خارجي لتحكيم الإجابات؛ الأحكام في هذا التقرير صادرة عن مراجعة Codex المحلية.",
    "التصنيف السابق المسجّل محفوظ كما هو للمقارنة، لكنه ليس حكم Codex الجديد وقد يتضمن أخطاء تحكيم قديمة.",
    "أُعيد تحكيم الحالات التي بدت كتراجع وفق المعيار نفسه، واستُخدم التشابه المرتفع لمنع احتساب إجابة شبه متطابقة كتراجع بسبب اختلاف وسم قديم.",
    "لا يحتوي التقرير على اقتراح إجابة أفضل، تنفيذًا لطلب المستخدم.",
    "عُدلت أحكام Q040 وQ098 وQ104 وQ114 وQ149 إلى صحيحة/مقبولة وفق حكم المستخدم والمعيار المرن.",
    "صُنفت Q151 وQ153 وQ154 كمشكلات تحتاج داتا للإصلاح وفق توجيه المستخدم، مع إبقاء حكم لقطة الاختبار كما هو.",
    "Q010 وQ139 نجحا في إعادة سؤال حية أجراها المستخدم بعد لقطة الاختبار؛ سُجل ذلك منفصلًا دون استبدال الإجابات التاريخية.",
    "Q120 كان مصنفًا صحيحًا أصلًا؛ لم تُغيّر Q121 لأن الملاحظة الرقمية المجاورة لـQ120 لم تحدد QID آخر بصورة مؤكدة.",
  ],
};

fs.mkdirSync(outputDir, { recursive: true });

const jsonlPath = path.join(outputDir, "codex_manual_evaluation_440.jsonl");
fs.writeFileSync(
  jsonlPath,
  `${evaluations.map((item) => JSON.stringify(item)).join("\n")}\n`,
  "utf8",
);

const summaryPath = path.join(outputDir, "codex_manual_evaluation_summary.json");
fs.writeFileSync(summaryPath, `${JSON.stringify(summary, null, 2)}\n`, "utf8");

const csvColumns = [
  "qid",
  "scenario_id",
  "turn",
  "difficulty",
  "question",
  "current_verdict",
  "score",
  "reason_ar",
  "judgment_basis",
  "live_recheck",
  "root_cause",
  "needs_data_addition",
  "previous_recorded_verdict",
  "previous_codex_verdict",
  "previous_codex_score",
  "comparison_code",
  "comparison_vs_recorded_code",
  "before_after_similarity",
  "before_answer",
  "after_answer",
  "top_sources",
  "selected_retrieval_sources",
  "rerank_status",
  "cache_hit",
  "before_latency_ms",
  "after_latency_ms",
  "search_query",
];
const csvRows = evaluations.map((item) => {
  const flat = {
    ...item,
    top_sources: item.metadata.top_chunk_sources,
    selected_retrieval_sources: item.metadata.selected_retrieval_sources,
    rerank_status: item.metadata.rerank_status,
    cache_hit: item.metadata.cache_hit,
    before_latency_ms: item.metadata.before_latency_ms,
    after_latency_ms: item.metadata.after_latency_ms,
    search_query: item.metadata.search_query,
  };
  return csvColumns.map((column) => csvCell(flat[column])).join(",");
});
const csvPath = path.join(outputDir, "codex_manual_evaluation_440.csv");
fs.writeFileSync(
  csvPath,
  `\uFEFF${csvColumns.map(csvCell).join(",")}\n${csvRows.join("\n")}\n`,
  "utf8",
);

const fmtPct = (value) => `${round(value, 2)}%`;
const fmtMs = (value) => Number.isFinite(value) ? `${round(value)} ms` : "—";
const qidInline = (items) => items.length ? items.map((item) => `\`${item.qid ?? item}\``).join("، ") : "لا يوجد";
const causeRows = Object.entries(rootCauseCounts)
  .sort(([, a], [, b]) => b - a)
  .map(([cause, count]) => `| ${rootCauseAr[cause] ?? cause} | ${count} |`)
  .join("\n");
const comparisonRows = Object.entries(comparisonCounts)
  .sort(([, a], [, b]) => b - a)
  .map(([code, count]) => {
    const sample = evaluations.find((item) => item.comparison_code === code)?.comparison_ar ?? code;
    return `| ${code} | ${count} | ${sample} |`;
  })
  .join("\n");

const report = [];
report.push("# تقرير تقييم Codex التفصيلي لإعادة اختبار 440 سؤالًا");
report.push("");
report.push(`- تاريخ التقييم: 2026-07-19`);
report.push(`- عدد الأسئلة: ${evaluations.length}`);
report.push("- المحكّم: Codex بمراجعة محلية؛ لم يُستخدم مفتاح API أو نموذج تحكيم خارجي.");
report.push("- المعيار: مرن؛ الإجابة المقبولة لا يلزم أن تكون مثالية، لكن يجب ألا تتضمن خطأً ماديًا.");
report.push("- أُدخلت تعديلات المستخدم الصريحة على الأحكام، ووُسمت داخل البيانات بـ user_lenient_override.");
report.push("- لا يتضمن هذا التقرير أي «إجابة أفضل»؛ فقط الحكم والدرجة والسبب والمقارنة والـmetadata.");
report.push("");
report.push("## الخلاصة");
report.push("");
report.push("| الحكم الحالي | العدد | النسبة |");
report.push("|---|---:|---:|");
report.push(`| صحيحة/مقبولة | ${verdictCounts.correct ?? 0} | ${fmtPct((verdictCounts.correct ?? 0) * 100 / evaluations.length)} |`);
report.push(`| مقبولة جزئيًا | ${verdictCounts.partial ?? 0} | ${fmtPct((verdictCounts.partial ?? 0) * 100 / evaluations.length)} |`);
report.push(`| خاطئة | ${verdictCounts.incorrect ?? 0} | ${fmtPct((verdictCounts.incorrect ?? 0) * 100 / evaluations.length)} |`);
report.push(`| قابلة للاستخدام (صحيح + جزئي) | ${(verdictCounts.correct ?? 0) + (verdictCounts.partial ?? 0)} | ${fmtPct(summary.current_usable_rate_pct)} |`);
report.push("");
report.push(`التصنيف السابق المسجّل كان: ${previousCounts.correct ?? 0} صحيح و${previousCounts.incorrect ?? 0} خطأ. بعد تطبيع الحالات المتعارضة بنفس معيار Codex المرن أصبح تقدير النسخة السابقة: ${previousCodexCounts.correct ?? 0} صحيحة، ${previousCodexCounts.partial ?? 0} جزئية، ${previousCodexCounts.incorrect ?? 0} خاطئة.`);
report.push("");
report.push("## المقارنة مع النسخة السابقة");
report.push("");
report.push("| رمز المقارنة | العدد | المعنى |");
report.push("|---|---:|---|");
report.push(comparisonRows);
report.push("");
report.push(`- تحسن فعلي وفق المعيار نفسه: ${improvements.length} سؤالًا — ${qidInline(improvements)}`);
report.push(`- تراجع فعلي إجمالًا وفق المعيار نفسه: ${regressions.length} سؤالًا — ${qidInline(regressions)}`);
report.push(`- من صحيحة سابقًا إلى خاطئة الآن: ${correctToIncorrect.length} سؤالًا — ${qidInline(correctToIncorrect)}`);
report.push(`- من صحيحة سابقًا إلى جزئية الآن: ${correctToPartial.length} سؤالًا — ${qidInline(correctToPartial)}`);
report.push(`- من جزئية سابقًا إلى خاطئة الآن: ${partialToIncorrect.length} سؤالًا — ${qidInline(partialToIncorrect)}`);
report.push(`- إجابات شبه متطابقة بين النسختين: ${nearUnchanged.length} سؤالًا؛ لم تُحتسب كتراجع لمجرد اختلاف الوسم التاريخي.`);
report.push("");
report.push("## التشخيص");
report.push("");
report.push(`- مشكلات لا تحتاج إضافة داتا: ${nonDataIssues.length}`);
report.push(`- مشكلات تحتاج داتا أو قاعدة رسمية إضافية قبل الجزم: ${dataGapIssues.length}`);
report.push(`- أسئلة مرتبطة بالاسترجاع أو دقة المصدر ضمن النتائج غير الصحيحة بالكامل: ${retrievalIssues.length}`);
report.push(`- من هذه الأسئلة، حالات لم ينجح فيها تطبيق ranker: ${rankerRelevant.length}`);
report.push(`- إصابات الكاش: ${summary.cache_hits} من 440 (الأسئلة فريدة، لذلك الصفر متوقع ولا يقيس فاعلية الكاش في الاستخدام المتكرر).`);
report.push(`- نجحت إعادة السؤال الحية بعد لقطة الاختبار في: ${qidInline(Object.keys(liveRechecks))}. هذه الملاحظة لا تستبدل الإجابة التاريخية المحفوظة.`);
report.push("");
report.push("| السبب الجذري | العدد |");
report.push("|---|---:|");
report.push(causeRows || "| لا توجد | 0 |");
report.push("");
report.push("### الأسئلة الخاطئة");
report.push("");
report.push(qidInline(incorrect));
report.push("");
report.push("### الأسئلة المقبولة جزئيًا");
report.push("");
report.push(qidInline(partial));
report.push("");
report.push("### أسئلة تحتاج داتا إضافية قبل إعطاء جواب حاسم");
report.push("");
report.push(qidInline(dataGapIssues));
report.push("");
report.push("### حالات استرجاع قد تستفيد من ranker ناجح");
report.push("");
report.push(qidInline(rankerRelevant));
report.push("");
report.push("## زمن الاستجابة وتشغيل المسار");
report.push("");
report.push("| القياس | النسخة السابقة | النسخة الجديدة |");
report.push("|---|---:|---:|");
report.push(`| المتوسط | ${fmtMs(summary.latency_ms.before.mean)} | ${fmtMs(summary.latency_ms.after.mean)} |`);
report.push(`| الوسيط | ${fmtMs(summary.latency_ms.before.median)} | ${fmtMs(summary.latency_ms.after.median)} |`);
report.push(`| P95 | ${fmtMs(summary.latency_ms.before.p95)} | ${fmtMs(summary.latency_ms.after.p95)} |`);
report.push("");
report.push(`تغير متوسط الزمن: ${fmtPct(summary.latency_ms.mean_change_pct)} (القيمة السالبة تعني تحسنًا). لا توجد أخطاء تقنية في ${evaluations.length - summary.technical_errors} من ${evaluations.length} إجابة.`);
report.push("");
report.push("## منهجية الحكم");
report.push("");
report.push("1. قُرئ السؤال مع تاريخ المحادثة المخزن وإجابة النسخة السابقة والجديدة.");
report.push("2. قورنت الادعاءات بالمقاطع والمصادر التي كانت متاحة للبوت، وعدد مجموعات المعرفة المسجل في الـtrace.");
report.push("3. عُدّت الإجابة صحيحة إذا أدت الغرض بلا خطأ مادي، حتى لو كانت مختصرة أو غير مثالية.");
report.push("4. استُخدم «جزئي» فقط عندما بقيت فائدة واضحة مع نقص رئيسي أو ادعاء غير موثق.");
report.push("5. أُعيد تحكيم حالات التراجع الظاهري على الإجابة السابقة بنفس المعيار، وفُصل الوسم التاريخي عن حكم Codex الموحّد حتى لا يُنسب خطأ تحكيم قديم إلى الإصلاح الجديد.");
report.push("");
report.push("## التقييم التفصيلي لكل سؤال");
report.push("");

for (const item of evaluations) {
  const meta = item.metadata;
  report.push(`### ${item.qid} — ${markdownText(item.question)}`);
  report.push("");
  report.push(`- الحكم: **${item.current_verdict_ar}**`);
  report.push(`- الدرجة: **${item.score}/100**`);
  report.push(`- السبب: ${item.reason_ar}`);
  report.push(`- أساس الحكم: ${item.judgment_basis === "user_lenient_override" ? "اعتماد المستخدم وفق المعيار المرن" : "مراجعة Codex المحلية"}`);
  if (item.live_recheck) {
    report.push(`- إعادة فحص حية بعد لقطة الاختبار: **صحيحة/مقبولة** — ${item.live_recheck.note}`);
  }
  report.push(`- السبب الجذري: ${item.root_cause_ar}`);
  report.push(`- يحتاج إضافة داتا: ${item.needs_data_addition ? "نعم" : "لا"}`);
  report.push(`- التصنيف السابق المسجّل تاريخيًا: ${item.previous_recorded_verdict === "correct" ? "صحيح" : "خطأ"}`);
  report.push(`- حكم Codex الموحّد على الإجابة السابقة: ${item.previous_codex_verdict_ar} (${item.previous_codex_score}/100)`);
  report.push(`- المقارنة بنفس المعيار: ${item.comparison_ar}`);
  report.push(`- تشابه نص الإجابتين: ${round(item.before_after_similarity * 100, 1)}%`);
  report.push(`- زمن الاستجابة: قبل ${fmtMs(meta.before_latency_ms)}، بعد ${fmtMs(meta.after_latency_ms)}`);
  report.push(`- الاسترجاع: ${meta.retrieval_strategy ?? "—"}؛ المرشحون ${meta.candidate_count ?? "—"}؛ المجموعات المسموحة ${meta.allowed_collection_count ?? "—"}`);
  report.push(`- ranker: ${meta.rerank_status}؛ مطلوب=${meta.rerank_requested ? "نعم" : "لا"}؛ جرت المحاولة=${meta.rerank_attempted ? "نعم" : "لا"}؛ الزمن=${fmtMs(meta.reranker_latency_ms)}`);
  report.push(`- cache_hit: ${meta.cache_hit ? "نعم" : "لا"}؛ عدد استدعاءات التوليد: ${meta.llm_call_count ?? "—"}؛ نموذج التوليد في الاختبار: ${meta.generation_model ?? "—"}`);
  report.push(`- ملفات/مجموعات المقاطع الأعلى: ${meta.top_chunk_sources.length ? meta.top_chunk_sources.join(" | ") : "—"}`);
  if (meta.selected_retrieval_sources.length) {
    report.push(`- مصادر ظهرت ضمن الاختيار النهائي للاسترجاع: ${meta.selected_retrieval_sources.join(" | ")}`);
  }
  report.push(`- استعلام الاسترجاع: ${meta.search_query ?? meta.base_query ?? "—"}`);
  report.push("");
  report.push("إجابة النسخة السابقة:");
  report.push("");
  report.push(renderAnswer(item.before_answer));
  report.push("");
  report.push("إجابة النسخة الجديدة:");
  report.push("");
  report.push(renderAnswer(item.after_answer));
  report.push("");
}

const reportPath = path.join(outputDir, "تقرير_تقييم_Codex_المفصل_440_قبل_وبعد.md");
fs.writeFileSync(reportPath, `${report.join("\n")}\n`, "utf8");

console.log(JSON.stringify({
  input: inputPath,
  output_dir: outputDir,
  outputs: {
    report: reportPath,
    jsonl: jsonlPath,
    summary: summaryPath,
    csv: csvPath,
  },
  summary,
}, null, 2));

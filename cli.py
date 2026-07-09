"""
Console test harness for the IUG Chatbot.

Commands inside the loop:
  exit / quit / خروج   → stop
  files                → list currently loaded uploaded files
  clear                → clear this session's chat history
  data                 → print which Mongo collections were loaded + counts
"""

from app.chatbot import IUGChatbot


def main():
    print("🚀 تشغيل IUG Chatbot — وضع الاختبار من الـ console")
    print("═" * 60)

    bot = IUGChatbot()
    try:
        bot.initialize()
    except Exception as exc:
        print(f"❌ فشل التهيئة (initialize): {exc}")
        raise SystemExit(1)

    print("═" * 60)
    print(f"📦 عدد الـ Collections المحمّلة: {len(bot.data)}")
    for col_name, docs in bot.data.items():
        print(f"   - {col_name}: {len(docs)} وثيقة")
    print(f"🧩 عدد الـ Chunks الكلي: {len(bot.chunks)}")
    print(f"📁 عدد الملفات المرفوعة المفهرسة: {len(bot.get_uploaded_files_list())}")
    print("═" * 60)

    session_id = input("🆔 أدخل session_id / رقم الطالب للاختبار (Enter لجلسة تجريبية): ").strip()
    if not session_id:
        session_id = "console_test_session"

    print("\n✅ الشات جاهز. اكتب سؤالك (أو 'exit' للخروج، 'files' لعرض الملفات، 'clear' لمسح السجل، 'data' لعرض بيانات الـ Collections).\n")

    while True:
        try:
            question = input("🧑 أنت: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n👋 وداعًا")
            break

        if not question:
            continue

        if question.lower() in ("exit", "quit", "خروج"):
            print("👋 وداعًا")
            break

        if question.lower() == "files":
            files = bot.get_uploaded_files_list()
            if not files:
                print("📁 لا توجد ملفات مرفوعة حالياً.")
            for f in files:
                print(f"   - {f['collection']}: {f['chunks_count']} مقطع | مفهرس: {f['indexed']}")
            continue

        if question.lower() == "clear":
            bot.clear_history(session_id)
            print("🧹 تم مسح سجل المحادثة لهذه الجلسة.")
            continue

        if question.lower() == "data":
            for col_name, docs in bot.data.items():
                print(f"   - {col_name}: {len(docs)} وثيقة")
            continue

        try:
            result = bot.chat_with_all_files(question, session_id)
            print(f"\n🤖 المساعد: {result['answer']}")
            print(f"   (عدد المقاطع المستخدمة كسياق: {len(result.get('top_chunks', []))})\n")
        except Exception as exc:
            print(f"❌ خطأ أثناء المحادثة: {exc}\n")


if __name__ == "__main__":
    main()

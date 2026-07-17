> ⚠️ هذا الملف تاريخي وقد تقادم جزئياً — المرجع المعتمد للنشر: README.md وrender.yaml.

# نشر تجريبي على Render (للفريق)

هدف هذا الدليل: رابط عام يبقى شغّالاً لأسابيع، يفتحه أي عضو من المتصفح ويجرّب
البوت عبر صفحة `/docs` التفاعلية — **بلا حاجة لواجهة أمامية بعد**.

الكود جاهز للنشر (`render.yaml` + `server.py` يربط على `$PORT`). الخطوات:

## 1) ارفع الكود إلى GitHub (مستودع خاص)

Render ينشر من مستودع Git. أنشئ مستودعاً **Private** (على حساب باسم مثلاً) ثم:

```bash
cd "C:/Users/ASUS/Desktop/باسم/Chatbot_IUG"
git remote add origin https://github.com/<الحساب>/iug-chatbot.git
git branch -M main
git push -u origin main
```

> ملف `.env` **لن يُرفع** (محميّ في `.gitignore`) — الأسرار تُضاف في Render مباشرة.

## 2) افتح Atlas للسماح لـ Render بالاتصال

في MongoDB Atlas → **Network Access** → أضِف `0.0.0.0/0` (السماح من أي مكان).
بدونها سيفشل الاتصال بالقاعدة عند النشر (عناوين Render متغيّرة).

## 3) أنشئ الخدمة على Render

1. [render.com](https://render.com) → **New** → **Web Service** → اربط مستودع GitHub.
2. Render سيقرأ `render.yaml` تلقائياً (Build/Start جاهزان).
3. في **Environment**، عبّئ الأسرار (انسخها من `.env` المحلي):
   `MONGO_URI`, `MONGO_DB_NAME`, `UPLOADED_DB_NAME`,
   `CHAT_API_URL`, `CHAT_API_KEY`, `CHAT_API_MODEL`,
   `EMBED_API_URL`, `EMBED_API_KEY`, `EMBED_MODEL`.
4. **Create Web Service** → انتظر البناء والإقلاع (أول إقلاع ~1–2 دقيقة: تحميل
   Mongo + بناء الفهارس عبر Jina).

## 4) جرّبوا

- الرابط: `https://<اسم-الخدمة>.onrender.com`
- **صفحة التجربة للفريق**: `https://<اسم-الخدمة>.onrender.com/docs`
  (جرّبوا `/api/chat/files` بكتابة سؤال والضغط Execute).
- فحص الجاهزية: `/health`.

## ملاحظات مهمة (خطة Render المجانية)

- **النوم عند الخمول**: بعد ~15 دقيقة بلا طلبات ينام الخادم؛ أول طلب بعده بطيء
  (~30 ثانية إقلاع). طبيعي في التجربة المجانية.
- **الكاش المؤقّت**: القرص يُمسح عند كل نشر، فتُعاد فهرسة الملفات عبر Jina عند
  الإقلاع (يستهلك بعض رصيد Jina). للنشر الرسمي لاحقاً نحفظ الفهرس في Mongo.
- **الأمان**: `API_ENV=development` يُبقي `/docs` مفتوحة للتجربة. عند النشر
  الرسمي بدّلها إلى `production` (تُخفى `/docs`) وحدّد `API_CORS_ORIGINS` برابط
  الواجهة الحقيقي بدل `*`.
- **لا مصادقة بعد**: أي شخص معه الرابط يستطيع الاستخدام — مقبول لتجربة داخلية،
  لكن لا تنشره علناً قبل إضافة المصادقة.

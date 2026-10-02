# نشر HelpDesk Pro على PythonAnywhere

دليل نشر العرض التجريبي العام على حساب PythonAnywhere المجاني، باستخدام Python 3.13 وخيار **Manual configuration**. التطبيق يعمل عبر WSGI مباشرة بالدالة `create_app`؛ لا حاجة إلى Gunicorn أو خادم آخر، فـPythonAnywhere يشغّل ملف WSGI بنفسه.

> العرض مشترك ودون حسابات: أي زائر يستطيع إنشاء البلاغات وتغيير حالاتها وإضافة الملاحظات. تظهر هذه الملاحظة داخل الواجهة في كل صفحة. لا يصلح لبيانات حقيقية.

استُخدمت في الإعداد التالي الوثائق الرسمية: [Flask على PythonAnywhere](https://help.pythonanywhere.com/pages/Flask/)، [متغيرات البيئة لتطبيقات الويب](https://help.pythonanywhere.com/pages/EnvironmentVariables/)، [فرض HTTPS](https://help.pythonanywhere.com/pages/ForcingHTTPS/)، [عنوان IP للزائر](https://help.pythonanywhere.com/pages/WebAppClientIPAddresses/)، [مزايا الحساب المجاني](https://help.pythonanywhere.com/pages/FreeAccountsFeatures/)، [إصدارات Python](https://help.pythonanywhere.com/pages/PythonVersions/)، ودليل Flask عن [أمان الكوكيز](https://flask.palletsprojects.com/en/stable/web-security/).

## متغيرات البيئة

| المتغير | القيمة | ملاحظات |
| --- | --- | --- |
| `HELPDESK_ENV` | `production` | يفعّل وضع النشر. بدونه يعمل التطبيق بالوضع المحلي. |
| `HELPDESK_SECRET_KEY` | مفتاح عشوائي طويل | **إلزامي في وضع النشر**، و32 حرفًا على الأقل؛ يرفض التطبيق البدء بدونه. يوقّع كوكي الجلسة ورموز CSRF. لا يُحفظ في Git. |
| `HELPDESK_DATABASE` | مسار مطلق لملف SQLite | **إلزامي في وضع النشر**؛ يرفض التطبيق أي مسار نسبي في كل الأوضاع. يُفضّل مجلد خارج مجلد الكود، مثل `/home/<اسم-المستخدم>/helpdesk-data/helpdesk.sqlite3`. يُنشأ المجلد والقاعدة تلقائيًا. |

ما يفعله وضع النشر تلقائيًا:

- يعطّل `debug` في مسار WSGI الموثق أدناه (`application = create_app()`) حتى لو ضُبط `FLASK_DEBUG`. لا يشمل ذلك التشغيل عبر Flask CLI مثل `flask --debug run`؛ لا تستخدمه في النشر.
- يضبط كوكي الجلسة بـ`Secure` و`HttpOnly` و`SameSite=Lax`، فلا تُرسل إلا عبر HTTPS. في الوضع المحلي يبقى `Secure` معطلًا ليعمل `http://127.0.0.1:5000`.
- يحدد هوية الزائر لحد الإرسال من آخر قيمة في `X-Forwarded-For` فقط. توضح وثائق PythonAnywhere أن هذه القيمة وحدها مضمونة، لأن موزع الأحمال يضيفها، أما القيم السابقة فيرسلها العميل وقد تكون مزورة. تُقسم الترويسة الخام عند الفواصل دون تحليل علامات الاقتباس، ثم يُتحقق من أن آخر قيمة عنوان IP صالح. إذا غابت أو لم تكن صالحة يُرفض النموذج برسالة عربية (HTTP 400) دون حفظ، ولا يُستخدم عنوان موزع الأحمال المشترك بين الزوار بديلًا. يُحفظ العنوان بصمةً مشفرة بـHMAC لا العنوان نفسه. في الوضع المحلي يُستخدم عنوان الاتصال المباشر.

## خطوات الإعداد

استبدل `<اسم-المستخدم>` باسم حسابك في كل الأوامر والمسارات.

### 1. الكود والبيئة الافتراضية

من تبويب **Consoles** افتح Bash:

```bash
git clone https://github.com/AbdullahKharaim/helpdesk-pro.git ~/helpdesk-pro
mkvirtualenv --python=/usr/bin/python3.13 helpdesk-pro
pip install -r ~/helpdesk-pro/requirements.txt
mkdir -p ~/helpdesk-data
```

Python 3.13 متاح في صورة النظام `innit`، وهي الافتراضية للحسابات الجديدة.

### 2. توليد المفتاح السري

في الـBash نفسه:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

انسخ الناتج إلى ملف WSGI في الخطوة 4 فقط. لا تضعه في المستودع أو في أي ملف داخل `~/helpdesk-pro`.

### 3. تطبيق الويب

1. من تبويب **Web** اختر **Add a new web app**، ثم **Manual configuration**، ثم **Python 3.13**.
2. في قسم **Code**:
   - **Source code:** `/home/<اسم-المستخدم>/helpdesk-pro`
   - **Working directory:** `/home/<اسم-المستخدم>/helpdesk-pro`
3. في قسم **Virtualenv:** `/home/<اسم-المستخدم>/.virtualenvs/helpdesk-pro`
4. في قسم **Static files** أضف: URL `/static/`، وDirectory `/home/<اسم-المستخدم>/helpdesk-pro/static/`. هذه الخطوة اختيارية، لكنها تجعل الموقع يقدّم ملف CSS مباشرة دون المرور بـFlask.

### 4. ملف WSGI

افتح ملف WSGI من رابطه في قسم **Code**، ويكون اسمه بالشكل `/var/www/<اسم-المستخدم>_pythonanywhere_com_wsgi.py`. احذف محتواه كله واستبدله بما يلي:

```python
import os
import sys

project = "/home/<اسم-المستخدم>/helpdesk-pro"
if project not in sys.path:
    sys.path.insert(0, project)

os.environ["HELPDESK_ENV"] = "production"
os.environ["HELPDESK_DATABASE"] = "/home/<اسم-المستخدم>/helpdesk-data/helpdesk.sqlite3"
os.environ["HELPDESK_SECRET_KEY"] = "<الصق هنا ناتج الخطوة 2>"

from app import create_app

application = create_app()
```

يقع ملف WSGI في `/var/www/` خارج المستودع، لذلك يبقى المفتاح خارج Git. لا تضف `app.run()`؛ فـPythonAnywhere يستورد `application` بنفسه. تقترح وثائق PythonAnywhere بديلًا عبر ملف `.env` وحزمة `python-dotenv`؛ يعمل هذا التطبيق بالطريقتين، لكن الطريقة أعلاه لا تحتاج إلى اعتمادية إضافية. إن استخدمت `.env` داخل مجلد المشروع فهو مستثنى من Git في `.gitignore`.

### 5. HTTPS والتشغيل

1. في قسم **Security** من تبويب **Web** فعّل **Force HTTPS**. نطاق `<اسم-المستخدم>.pythonanywhere.com` يعمل بـHTTPS دون إعداد شهادة.
2. اضغط **Reload** في أعلى تبويب **Web**.
3. افتح `https://<اسم-المستخدم>.pythonanywhere.com/` وجرّب: إنشاء بلاغ، ونقله إلى «قيد المعالجة» ثم «تم الحل»، وإضافة ملاحظة.
4. إذا ظهر خطأ فراجع **Error log** من تبويب **Web**. غياب المفتاح أو مسار القاعدة يظهر فيه برسالة واضحة، ويرفض التطبيق البدء.

## التجديد الشهري

تطبيق الويب في الحساب المجاني يتوقف إذا لم يُجدَّد شهريًا. قبل انتهاء المدة سجّل الدخول، وافتح تبويب **Web**، واضغط **Run until 1 month from today**. يصل تذكير بالبريد قبل الانتهاء، ويمكن إعادة التشغيل من الزر نفسه إذا توقف التطبيق. لاحظ أيضًا حدود الحساب المجاني: تطبيق ويب واحد بعامل واحد، ومساحة 512 ميغابايت.

## التحديث وإعادة الضبط

- **تحديث الكود:** `cd ~/helpdesk-pro && git pull`، ثم **Reload**. إذا تغيّر `requirements.txt` فشغّل قبلها `workon helpdesk-pro` ثم `pip install -r requirements.txt`.
- **تفريغ بيانات العرض:** احذف ملف القاعدة المحدد في `HELPDESK_DATABASE`، ثم **Reload**؛ تُنشأ قاعدة فارغة تلقائيًا. هذا يحذف كل البلاغات والملاحظات نهائيًا.
- **تغيير المفتاح السري:** عدّل القيمة في ملف WSGI ثم **Reload**. تنتهي صلاحية النماذج المفتوحة لدى الزوار، ويحصلون على رسالة تطلب إعادة الإرسال.

## الحدود والحماية في العرض

| الحماية | القيمة الافتراضية | ما يراه الزائر عند تجاوزها |
| --- | --- | --- |
| رمز CSRF في نماذج الإنشاء وتغيير الحالة والملاحظة | صالح لساعة، ومرتبط بجلسة الزائر | رسالة عربية تفرّق بين انتهاء صلاحية النموذج ورفض الرمز، مع بقاء ما كتبه ورمز جديد لإعادة الإرسال (HTTP 400). |
| حجم الطلب | 64 كيلوبايت | صفحة خطأ عربية؛ لا يُحفظ شيء (HTTP 413). |
| معدل الإرسال لكل زائر | 10 نماذج في الدقيقة، تشمل الأنواع الثلاثة | رسالة عربية مع بقاء المدخلات، وترويسة `Retry-After` (HTTP 429). إذا تعذّر تحديد عنوان الزائر في وضع النشر يُرفض النموذج (HTTP 400). |
| عدد البلاغات | 200 بلاغ | رسالة بأن العرض بلغ الحد الأقصى، مع بقاء المدخلات (HTTP 409). |
| عدد الملاحظات | 30 ملاحظة لكل بلاغ | رسالة عند نموذج الملاحظة، مع بقاء النص (HTTP 409). |

يُفحص حد الإرسال ويُسجَّل في معاملة SQLite مستقلة من نوع `BEGIN IMMEDIATE`. بعد نجاحه والتحقق من الحقول، يُفحص حد البلاغات أو الملاحظات ويُحفظ البلاغ أو الملاحظة في معاملة `BEGIN IMMEDIATE` ثانية منفصلة. كل فحص يقع مع الكتابة التي يحميها في المعاملة نفسها، فلا تتجاوز الطلبات المتزامنة أيًا من الحدين. المعاملتان ليستا ذرّيتين معًا: محاولة رُفضت عند التحقق أو عند حد العدد تبقى محسوبة من حصة الإرسال. القيم قابلة للتعديل من إعدادات `create_app` (`MAX_CONTENT_LENGTH` و`WRITE_RATE_LIMIT` و`WRITE_RATE_WINDOW` و`MAX_TICKETS` و`MAX_NOTES_PER_TICKET` و`CSRF_TIME_LIMIT`). عند تشغيل نسخة موجودة لأول مرة على هذا الكود يُضاف إلى القاعدة جدول `write_attempts` الصغير لحد المعدل، ولا تتغير جداول البلاغات أو الملاحظات.

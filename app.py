from flask import Flask

app = Flask(__name__)


@app.route("/")
def home():
    return "<h1>HelpDesk Pro</h1><p>النسخة المحلية قيد البناء.</p>"
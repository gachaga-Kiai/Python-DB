import os
import io
from flask import Flask, render_template, request, redirect, url_for, flash, send_file

import db
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")
# Allow uploads up to ~30 MB per request (attachments are capped at 25 MB each in db.py)
app.config["MAX_CONTENT_LENGTH"] = 30 * 1024 * 1024

CATEGORIES = ["Contact", "Account / Login", "Financial", "Medical", "Other"]

db.init_db()

# Automated daily backup job
scheduler = BackgroundScheduler()
scheduler.add_job(db.create_backup, "interval", hours=24, id="daily_backup", replace_existing=True)
scheduler.start()

# Make sure there's at least one backup on first run
if not db.list_backups():
    db.create_backup()


@app.route("/")
def index():
    query = request.args.get("q", "").strip().lower()
    category_filter = request.args.get("category", "")
    records = db.get_all_records()

    if category_filter:
        records = [r for r in records if r["category"] == category_filter]

    if query:
        records = [
            r
            for r in records
            if query in r["name"].lower()
            or query in r["email"].lower()
            or query in r["phone"].lower()
            or query in r["notes"].lower()
        ]

    return render_template(
        "index.html",
        records=records,
        categories=CATEGORIES,
        query=query,
        category_filter=category_filter,
    )


@app.route("/add", methods=["GET", "POST"])
def add():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Name is required.", "error")
            return redirect(url_for("add"))
        db.add_record(
            request.form.get("category", "Other"),
            name,
            request.form.get("email", "").strip(),
            request.form.get("phone", "").strip(),
            request.form.get("notes", "").strip(),
        )
        flash("Record added and encrypted successfully.", "success")
        return redirect(url_for("index"))
    return render_template("form.html", categories=CATEGORIES, record=None)


@app.route("/edit/<int:record_id>", methods=["GET", "POST"])
def edit(record_id):
    record = db.get_record(record_id)
    if not record:
        flash("Record not found.", "error")
        return redirect(url_for("index"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Name is required.", "error")
            return redirect(url_for("edit", record_id=record_id))
        db.update_record(
            record_id,
            request.form.get("category", "Other"),
            name,
            request.form.get("email", "").strip(),
            request.form.get("phone", "").strip(),
            request.form.get("notes", "").strip(),
        )
        flash("Record updated.", "success")
        return redirect(url_for("index"))

    return render_template("form.html", categories=CATEGORIES, record=record)


@app.route("/delete/<int:record_id>", methods=["POST"])
def delete(record_id):
    db.delete_record(record_id)
    flash("Record deleted.", "success")
    return redirect(url_for("index"))


@app.route("/record/<int:record_id>/upload", methods=["POST"])
def upload_attachment(record_id):
    record = db.get_record(record_id)
    if not record:
        flash("Record not found.", "error")
        return redirect(url_for("index"))

    file = request.files.get("file")
    if not file or not file.filename:
        flash("Please choose a file to upload.", "error")
        return redirect(url_for("edit", record_id=record_id))

    data = file.read()
    if not data:
        flash("That file appears to be empty.", "error")
        return redirect(url_for("edit", record_id=record_id))

    try:
        db.add_attachment(record_id, file.filename, file.mimetype, data)
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("edit", record_id=record_id))

    flash("File encrypted and attached successfully.", "success")
    return redirect(url_for("edit", record_id=record_id))


@app.route("/attachment/<int:attachment_id>/download")
def download_attachment(attachment_id):
    att = db.get_attachment_file(attachment_id)
    if not att:
        flash("File not found.", "error")
        return redirect(url_for("index"))
    return send_file(
        io.BytesIO(att["data"]),
        mimetype=att["content_type"] or "application/octet-stream",
        as_attachment=True,
        download_name=att["filename"],
    )


@app.route("/attachment/<int:attachment_id>/delete", methods=["POST"])
def delete_attachment(attachment_id):
    record_id = request.form.get("record_id")
    db.delete_attachment(attachment_id)
    flash("Attachment deleted.", "success")
    if record_id:
        return redirect(url_for("edit", record_id=record_id))
    return redirect(url_for("index"))


@app.route("/backups")
def backups():
    return render_template("backups.html", backups=db.list_backups())


@app.route("/backups/create", methods=["POST"])
def backups_create():
    name = db.create_backup()
    if name:
        flash(f"Backup created: {name}", "success")
    else:
        flash("No database to back up yet.", "error")
    return redirect(url_for("backups"))


@app.route("/backups/download/<name>")
def backups_download(name):
    safe_name = os.path.basename(name)
    path = os.path.join(db.BACKUP_DIR, safe_name)
    if not os.path.exists(path):
        flash("Backup file not found.", "error")
        return redirect(url_for("backups"))
    return send_file(path, as_attachment=True)


@app.errorhandler(413)
def file_too_large(e):
    flash("That file is too large. The maximum attachment size is 25 MB.", "error")
    return redirect(request.referrer or url_for("index"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True, use_reloader=False)

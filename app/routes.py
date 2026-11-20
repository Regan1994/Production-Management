import tempfile
import hmac
import secrets
import sqlite3
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, send_file, session, url_for

from .barcode import code128_svg
from .db import get_db
from .services import ASSEMBLY_STAGES, DEBUG_CLEAR_CHOICES, access_password_enabled, add_sales_order_line, assign_machine, assign_machines, backup_database, cancel_sales_order, change_access_password, clear_debug_data, correct_actual_qty, create_assembly_worker, create_batch, create_model, create_sales_order, debug_clear_counts, delete_batch, delete_model, delete_sales_order_line, delete_sales_order, ensure_barcodes, export_xlsx, get_assembly_worker, get_batch, get_job, get_sales_order, is_five_axis_model, list_assembly_workers, list_available_inventory_machines, list_models, list_order_assignments, list_order_lines, list_recent_batches, list_report_operators, list_sales_orders, mark_machine_painted, mark_machine_painted_by_serial, parse_payload, production_inventory, query_report, reassign_machine, restore_database, scan, set_access_password_enabled, set_assembly_worker_active, summarize_report_by_operator, unassign_machine, update_model, update_sales_order, update_sales_order_line, verify_access_password

bp = Blueprint("main", __name__)

PROTECTED_ENDPOINTS = {
    "main.models",
    "main.model_update",
    "main.model_delete",
    "main.assembly_workers",
    "main.assembly_worker_status",
    "main.recent_batches",
    "main.batch_delete",
    "main.reports",
    "main.reports_export",
    "main.production_management",
    "main.orders",
    "main.order_new",
    "main.order_line_create",
    "main.order_edit",
    "main.order_update",
    "main.order_line_update",
    "main.order_line_delete",
    "main.order_cancel",
    "main.order_delete",
    "main.order_assignment",
    "main.order_line_assign",
    "main.machine_assign",
    "main.machine_unassign",
    "main.machine_reassign",
    "main.machine_paint_complete",
    "main.machine_paint_complete_by_serial",
    "main.quantity_update",
    "main.settings",
    "main.change_password",
    "main.password_mode",
    "main.debug_clear",
    "main.backup",
    "main.restore",
}


def safe_next_path(value):
    return value if value and value.startswith("/") and not value.startswith("//") else url_for("main.index")


@bp.before_request
def require_admin_unlock():
    if request.endpoint not in PROTECTED_ENDPOINTS or not access_password_enabled() or session.get("admin_unlocked"):
        return None
    next_path = request.full_path.rstrip("?")
    return redirect(url_for("main.unlock", next=next_path))


def render_home(scan_result=None):
    stats = get_db().execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN status IN ('created','started') THEN 1 ELSE 0 END), 0) AS incomplete,
            COALESCE(SUM(CASE WHEN status='finished' THEN 1 ELSE 0 END), 0) AS finished,
            COALESCE(SUM(CASE WHEN status='cancelled' THEN 1 ELSE 0 END), 0) AS cancelled
        FROM job_run
        """
    ).fetchone()
    cards = [
        {"label": "未完成", "count": stats["incomplete"], "filter": "incomplete"},
        {"label": "已完成", "count": stats["finished"], "filter": "finished"},
        {"label": "已取消", "count": stats["cancelled"], "filter": "cancelled"},
    ]
    return render_template("index.html", stats=cards, scan_result=scan_result)


@bp.route("/", methods=["GET", "POST"])
def index():
    if request.method == "GET":
        session.pop("admin_unlocked", None)
    return render_template("system_home.html")


@bp.get("/assembly")
def assembly_home():
    return render_home()


@bp.route("/unlock", methods=["GET", "POST"])
def unlock():
    next_path = safe_next_path(request.form.get("next") if request.method == "POST" else request.args.get("next"))
    if request.method == "POST":
        if verify_access_password(request.form.get("password", "")):
            session["admin_unlocked"] = True
            return redirect(next_path)
        flash("密碼錯誤，請重新輸入", "error")
    return render_template("unlock.html", next_path=next_path)


@bp.route("/models", methods=["GET", "POST"])
def models():
    if request.method == "POST":
        try:
            create_model(request.form.get("model_name"), description=request.form.get("description", ""), operator_name=request.form.get("operator_name", "admin"), serial_prefix=request.form.get("serial_prefix"))
            flash("機型已建立", "success")
        except ValueError as exc:
            flash(str(exc), "error")
    token = session.get("model_delete_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["model_delete_token"] = token
    return render_template("models.html", models=list_models(), delete_token=token)


@bp.post("/models/<int:model_id>/update")
def model_update(model_id):
    try:
        update_model(model_id, request.form.get("model_name"), description=request.form.get("description", ""), is_active=request.form.get("is_active") == "1", operator_name=request.form.get("operator_name", "admin"), serial_prefix=request.form.get("serial_prefix"))
        flash("機型已更新", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.models"))


@bp.post("/models/<int:model_id>/delete")
def model_delete(model_id):
    token = session.get("model_delete_token", "")
    if not token or not hmac.compare_digest(token, request.form.get("csrf_token", "")):
        flash("刪除確認已失效，請重新開啟機型主檔", "error")
        return redirect(url_for("main.models"))
    try:
        backup = delete_model(model_id, request.form.get("expected_name", ""), current_app.config["BACKUP_DIRECTORY"])
        session.pop("model_delete_token", None)
        flash(f"機型已刪除；資料庫備份：{backup.name}", "success")
    except (ValueError, OSError, sqlite3.Error) as exc:
        flash(f"沒有刪除機型：{exc}", "error")
    return redirect(url_for("main.models"))


@bp.route("/machines", methods=["GET", "POST"])
def machines():
    flash("新產品報工不需要先建立機台主檔，請直接從建立報工輸入機型、年份、起始流水號與台數。", "info")
    return redirect(url_for("main.job_new"))


@bp.post("/machines/<int:machine_id>/update")
def machine_update(machine_id):
    flash("新流程不再維護機台主檔；舊機台資料僅保留供歷史資料相容。", "info")
    return redirect(url_for("main.job_new"))


@bp.route("/assembly-workers", methods=["GET", "POST"])
def assembly_workers():
    if request.method == "POST":
        try:
            create_assembly_worker(request.form.get("name"), request.form.get("changed_by", "admin"))
            flash("組裝人員已新增", "success")
        except ValueError as exc:
            flash(str(exc), "error")
    return render_template("assembly_workers.html", workers=list_assembly_workers())


@bp.post("/assembly-workers/<int:worker_id>/status")
def assembly_worker_status(worker_id):
    try:
        set_assembly_worker_active(worker_id, request.form.get("is_active") == "1", request.form.get("changed_by", "admin"))
        flash("組裝人員狀態已更新", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.assembly_workers"))


@bp.get("/jobs/new")
def job_new():
    model_id = request.args.get("model_id")
    models = list_models(True)
    selected = next((model for model in models if str(model["id"]) == str(model_id)), None)
    return render_template("job_new.html", models=models, workers=list_assembly_workers(True), selected_model=model_id, selected_model_record=selected, is_five_axis=bool(selected and is_five_axis_model(selected)), assembly_stages=ASSEMBLY_STAGES, current_year=datetime.now().year)


@bp.post("/jobs")
def job_create():
    try:
        model_id = int(request.form["model_id"])
        worker = get_assembly_worker(request.form.get("operator_name", ""))
        if not worker:
            raise ValueError("請先選擇有效的組裝人員；如清單沒有該人員，請先新增組裝人員")
        batch_id, _ = create_batch(model_id, request.form.get("manufacturing_year"), request.form.get("starting_number"), request.form.get("product_count"), worker["name"], request.form.get("assembly_stage"))
        ensure_barcodes(batch_id)
        return redirect(url_for("main.print_batch", batch_id=batch_id))
    except (ValueError, KeyError) as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.job_new", model_id=request.form.get("model_id")))


@bp.get("/batches/<batch_id>/print")
def print_batch(batch_id):
    batch = get_batch(batch_id)
    if not batch:
        return "找不到批次", 404
    jobs = ensure_barcodes(batch_id)
    records = get_db().execute("SELECT * FROM barcode_record WHERE batch_id=? OR job_run_id IN (SELECT job_run_id FROM job_run WHERE batch_id=?) ORDER BY barcode_scope, CASE barcode_type WHEN 'START' THEN 0 WHEN 'FINISH' THEN 1 ELSE 2 END, job_run_id", (batch_id, batch_id)).fetchall()
    return render_template("print_batch.html", batch=batch, jobs=jobs, records=records, code128_svg=code128_svg)


@bp.get("/batches/recent")
def recent_batches():
    token = session.get("batch_delete_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["batch_delete_token"] = token
    return render_template("batches.html", batches=list_recent_batches(), csrf_token=token)


@bp.post("/batches/<batch_id>/delete")
def batch_delete(batch_id):
    token = session.get("batch_delete_token", "")
    if not token or not hmac.compare_digest(token, request.form.get("csrf_token", "")):
        flash("刪除確認已失效，請重新開啟最近批次", "error")
        return redirect(url_for("main.recent_batches"))
    try:
        backup, removed = delete_batch(
            batch_id,
            request.form.get("expected_batch_id", ""),
            request.form.get("confirmation", ""),
            current_app.config["BACKUP_DIRECTORY"],
        )
        flash(f"批次已刪除；已先備份至 {backup.name}。刪除筆數：{sum(removed.values())}", "success")
    except (ValueError, OSError, sqlite3.Error) as exc:
        flash(f"沒有刪除批次：{exc}", "error")
    return redirect(url_for("main.recent_batches"))


@bp.route("/scan", methods=["GET", "POST"])
def scan_page():
    if request.method == "POST":
        payload = request.form.get("payload", "")
        result = scan(payload, "scanner")
        result["payload"] = payload
        return render_template("scan.html", result=result)
    return render_template("scan.html", result=None)


@bp.post("/api/scan")
def api_scan():
    result = scan(request.json.get("payload", "") if request.is_json else request.form.get("payload", ""), request.json.get("operator_name", "scanner") if request.is_json else request.form.get("operator_name", "scanner"))
    return jsonify(result), 200 if result["result"] in {"success", "duplicate"} else 400


@bp.route("/reports", methods=["GET", "POST"])
def reports():
    filters = request.form.to_dict() if request.method == "POST" else request.args.to_dict()
    rows = query_report(filters)
    return render_template("reports.html", rows=rows, summaries=summarize_report_by_operator(rows), filters=filters, operators=list_report_operators())


@bp.post("/reports/export")
def reports_export():
    filters = request.form.to_dict()
    rows = query_report(filters)
    tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
    tmp.close()
    export_xlsx(rows, tmp.name)
    filename = "INTJR_報工報表_" + datetime.now().strftime("%Y%m%d_%H%M") + ".xlsx"
    return send_file(tmp.name, as_attachment=True, download_name=filename)


@bp.get("/production-management")
def production_management():
    model_id = request.args.get("model_id", type=int)
    summary, machines = production_inventory()
    stock_models = [row for row in summary if row["available_count"]]
    if model_id:
        summary = [row for row in summary if row["model_id"] == model_id]
        machines = [machine for machine in machines if machine["model_id"] == model_id]
    return render_template(
        "production_management.html",
        available_machines=[machine for machine in machines if model_id and machine["painted_at"] and not machine["assignment_id"]],
        inventory_summary=[row for row in summary if model_id and row["available_count"]],
        stock_models=stock_models,
        selected_model_id=model_id,
        today=date.today().isoformat(),
    )


@bp.get("/assignment")
def assignment_home():
    return render_template("assignment_home.html")


@bp.get("/completed-assignments")
def completed_assignments():
    return redirect(url_for("main.orders", view="completed"))


def create_order_from_request(destination):
    try:
        model_ids = request.form.getlist("model_id")
        quantities = request.form.getlist("required_qty")
        lines = [(model_id, quantity) for model_id, quantity in zip(model_ids, quantities) if model_id or quantity]
        create_sales_order(
            request.form.get("order_no"),
            "",
            request.form.get("operator_name", "admin"),
            lines,
            customer_name=request.form.get("customer_name"),
            expected_ship_date=request.form.get("expected_ship_date"),
        )
        flash("需求單與機型需求已輸入", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for(destination))


@bp.get("/orders")
def orders():
    view = request.args.get("view", "pending")
    if view not in {"pending", "completed", "history"}:
        view = "pending"
    query = (request.args.get("q") or "").strip().lower()
    source_orders = list_sales_orders(active_only=view != "history")
    if view == "pending":
        orders = [order for order in source_orders if order["assigned_qty"] < order["required_qty"]]
    elif view == "completed":
        orders = [order for order in source_orders if order["required_qty"] and order["assigned_qty"] >= order["required_qty"]]
    else:
        orders = [order for order in source_orders if not order["is_active"]]
    if query:
        orders = [order for order in orders if query in order["order_no"].lower() or query in (order["customer_name"] or "").lower()]
    pending_lines_by_order = defaultdict(list)
    for line in list_order_lines(unassigned_only=True):
        pending_lines_by_order[line["order_id"]].append(line)
    return render_template(
        "orders.html",
        orders=orders,
        selected_view=view,
        order_query=query,
        pending_lines_by_order=pending_lines_by_order,
    )


@bp.route("/orders/new", methods=["GET", "POST"])
def order_new():
    if request.method == "POST":
        return create_order_from_request("main.order_new")
    return render_template("order_new.html", models=list_models(True))


@bp.post("/orders")
def order_create_legacy():
    return create_order_from_request("main.orders")


@bp.post("/orders/<int:order_id>/lines")
def order_line_create(order_id):
    try:
        add_sales_order_line(order_id, int(request.form["model_id"]), request.form.get("required_qty"), request.form.get("operator_name", "admin"))
        flash("需求單明細已新增", "success")
    except (ValueError, KeyError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.orders"))


@bp.get("/orders/<int:order_id>/edit")
def order_edit(order_id):
    order = get_sales_order(order_id)
    if not order or not order["is_active"]:
        return "找不到有效訂單", 404
    assignments_by_line = defaultdict(list)
    for assignment in list_order_assignments(order_id):
        assignments_by_line[assignment["order_line_id"]].append(assignment)
    return render_template(
        "order_edit.html",
        order=order,
        lines=list_order_lines(order_id=order_id),
        models=list_models(True),
        assignments_by_line=assignments_by_line,
    )


@bp.post("/orders/<int:order_id>/update")
def order_update(order_id):
    try:
        update_sales_order(order_id, request.form.get("order_no"), request.form.get("customer_name"), request.form.get("expected_ship_date"), request.form.get("operator_name", "admin"))
        flash("訂單基本資料已更新", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.order_edit", order_id=order_id))


@bp.post("/orders/<int:order_id>/lines/<int:line_id>/update")
def order_line_update(order_id, line_id):
    try:
        update_sales_order_line(order_id, line_id, request.form.get("model_id"), request.form.get("required_qty"), request.form.get("operator_name", "admin"))
        flash("訂單機型需求已更新", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.order_edit", order_id=order_id))


@bp.post("/orders/<int:order_id>/lines/<int:line_id>/delete")
def order_line_delete(order_id, line_id):
    try:
        delete_sales_order_line(order_id, line_id, request.form.get("operator_name", "admin"))
        flash("訂單機型需求已移除", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.order_edit", order_id=order_id))


@bp.post("/orders/<int:order_id>/cancel")
def order_cancel(order_id):
    try:
        cancel_sales_order(order_id, request.form.get("operator_name", "admin"))
        flash("訂單已取消；已配機台已退回可用庫存", "success")
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.order_edit", order_id=order_id))
    return redirect(url_for("main.orders"))


@bp.post("/orders/<int:order_id>/delete")
def order_delete(order_id):
    try:
        delete_sales_order(order_id, request.form.get("operator_name", "admin"))
        flash("訂單已永久刪除；已配機台已退回可用庫存", "success")
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.order_edit", order_id=order_id))
    return redirect(url_for("main.orders"))


@bp.get("/orders/<int:order_id>/assignment")
def order_assignment(order_id):
    order = get_sales_order(order_id)
    if not order:
        return "找不到需求單", 404
    lines = list_order_lines(order_id=order_id)
    available_by_model = {line["model_id"]: list_available_inventory_machines(line["model_id"]) for line in lines}
    return render_template("order_assignment.html", order=order, lines=lines, available_by_model=available_by_model)


@bp.post("/orders/<int:order_id>/lines/<int:line_id>/assign")
def order_line_assign(order_id, line_id):
    try:
        line = next((item for item in list_order_lines(order_id=order_id) if item["id"] == line_id), None)
        if not line:
            raise ValueError("找不到此需求單的機型需求")
        count = assign_machines(request.form.getlist("machine_id"), line_id, request.form.get("operator_name", "admin"))
        flash(f"已將 {count} 台 {line['model_name']} 機台配給需求單 {line['order_no']}", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.order_assignment", order_id=order_id))


@bp.post("/production-management/machines/<int:machine_id>/assign")
def machine_assign(machine_id):
    try:
        assign_machine(machine_id, int(request.form["order_line_id"]), request.form.get("operator_name", "admin"))
        flash("製號已配給需求單", "success")
    except (ValueError, KeyError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.production_management", model_id=request.form.get("model_id") or None))


@bp.post("/production-management/assignments/<int:assignment_id>/unassign")
def machine_unassign(assignment_id):
    try:
        unassign_machine(assignment_id, request.form.get("operator_name", "admin"), request.form.get("reason"))
        flash("配單已取消，製號已退回已噴漆可用庫存", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    order_id = request.form.get("order_id", type=int)
    if order_id:
        return redirect(url_for("main.order_edit", order_id=order_id))
    return redirect(url_for("main.orders", view="completed"))


@bp.post("/production-management/assignments/<int:assignment_id>/reassign")
def machine_reassign(assignment_id):
    try:
        reassign_machine(assignment_id, int(request.form["order_line_id"]), request.form.get("operator_name", "admin"), request.form.get("reason", "改配"))
        flash("製號已改配；原需求單明細已自動回復", "success")
    except (ValueError, KeyError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.production_management", model_id=request.form.get("model_id") or None))


@bp.post("/production-management/machines/<int:machine_id>/paint-complete")
def machine_paint_complete(machine_id):
    try:
        mark_machine_painted(machine_id, request.form.get("painted_at"), request.form.get("operator_name", "admin"))
        flash("已登記噴漆完成，機台已入庫並列入可配單庫存", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.production_management", model_id=request.form.get("model_id") or None))


@bp.post("/production-management/paint-complete")
def machine_paint_complete_by_serial():
    try:
        machine = mark_machine_painted_by_serial(
            request.form.get("product_serial"),
            request.form.get("painted_at"),
            request.form.get("operator_name", "admin"),
        )
        flash(f"{machine['product_serial']}（{machine['model_name_snapshot']}）已登記噴漆完成並入庫", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.production_management", model_id=request.form.get("model_id") or None))


@bp.post("/jobs/<job_run_id>/quantity")
def quantity_update(job_run_id):
    try:
        correct_actual_qty(job_run_id, request.form.get("actual_qty"), request.form.get("operator_name", "admin"))
        flash("完成量已更正並寫入稽核紀錄", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.reports"))


@bp.get("/settings")
def settings():
    return render_template("settings.html", password_enabled=access_password_enabled())


@bp.post("/settings/password")
def change_password():
    try:
        new_password = request.form.get("new_password", "")
        if new_password != request.form.get("confirm_password", ""):
            raise ValueError("兩次輸入的新密碼不一致")
        change_access_password(request.form.get("current_password", ""), new_password)
        flash("密碼已更新", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.settings"))


@bp.post("/settings/password-mode")
def password_mode():
    enabled = request.form.get("password_enabled") == "1"
    set_access_password_enabled(enabled)
    if not enabled:
        session.pop("admin_unlocked", None)
        flash("功能密碼已暫時停用，方便排查", "success")
    else:
        flash("功能密碼已重新啟用", "success")
    return redirect(url_for("main.settings"))


@bp.route("/debug/clear", methods=["GET", "POST"])
def debug_clear():
    if request.method == "POST":
        token = session.get("debug_clear_token", "")
        if not token or not hmac.compare_digest(token, request.form.get("csrf_token", "")):
            flash("清除確認已失效，請重新開啟頁面", "error")
            return redirect(url_for("main.debug_clear"))
        try:
            backup, removed = clear_debug_data(
                request.form.getlist("items"),
                request.form.get("confirmation", ""),
                current_app.config["BACKUP_DIRECTORY"],
            )
            session.pop("debug_clear_token", None)
            flash(f"清除完成；已先備份至 {backup}。清除筆數：{sum(removed.values())}", "success")
        except (ValueError, OSError, sqlite3.Error) as exc:
            flash(f"沒有清除資料：{exc}", "error")
        return redirect(url_for("main.debug_clear"))
    token = session.get("debug_clear_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["debug_clear_token"] = token
    return render_template("debug_clear.html", counts=debug_clear_counts(), choices=DEBUG_CLEAR_CHOICES, csrf_token=token)


@bp.route("/backup", methods=["GET", "POST"])
def backup():
    try:
        target = backup_database(current_app.config["BACKUP_DIRECTORY"])
        flash(f"備份完成：{target.name}", "success")
    except Exception as exc:
        flash(f"備份失敗：{exc}", "error")
    return redirect(url_for("main.index"))


@bp.post("/restore")
def restore():
    source = request.form.get("source_path", "").strip()
    try:
        restore_database(source, current_app.config["DATABASE"], current_app.config["BACKUP_DIRECTORY"], request.form.get("operator_name", "admin"))
        flash("還原完成，請重新啟動應用程式以重新建立資料庫連線", "success")
    except Exception as exc:
        flash(f"還原失敗：{exc}", "error")
    return redirect(url_for("main.settings"))


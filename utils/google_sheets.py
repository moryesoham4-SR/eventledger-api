import threading
import json
import urllib.request
import urllib.parse
from core.database import execute
from utils.db_safety import run_safely

GOOGLE_APPS_SCRIPT_TEMPLATE = """/**
 * EventLedger AI — Google Sheets Live Auto-Sync Webhook Script
 * 
 * Instructions:
 * 1. Open your Google Sheet.
 * 2. Click Extensions -> Apps Script.
 * 3. Delete any existing code and paste this entire code script.
 * 4. Click 'Deploy' -> 'New deployment'.
 * 5. Select type: 'Web app'.
 * 6. Set 'Execute as': 'Me'.
 * 7. Set 'Who has access': 'Anyone'.
 * 8. Click 'Deploy', authorize access, and copy the Web App URL!
 * 9. Paste the Web App URL into EventLedger Settings -> Google Sheets Integration.
 */

function doPost(e) {
  try {
    var data = JSON.parse(e.postData.contents);
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    
    if (data.action === "sync_all") {
      syncFullEventLedger(ss, data);
      return ContentService.createTextOutput(JSON.stringify({ status: "success", message: "Full EventLedger synced successfully!" })).setMimeType(ContentService.MimeType.JSON);
    }
    
    appendSingleRecord(ss, data);
    return ContentService.createTextOutput(JSON.stringify({ status: "success", message: "Record updated" })).setMimeType(ContentService.MimeType.JSON);
  } catch (err) {
    return ContentService.createTextOutput(JSON.stringify({ status: "error", message: err.toString() })).setMimeType(ContentService.MimeType.JSON);
  }
}

function syncFullEventLedger(ss, payload) {
  // 1. Financial Summary Tab
  var summarySheet = getOrCreateSheet(ss, "📊 Financial Summary");
  summarySheet.clear();
  var summaryRows = [
    ["Event Name", payload.event_name || "EventLedger AI", "", ""],
    ["Last Synced", new Date().toLocaleString(), "", ""],
    ["", "", "", ""],
    ["Metric", "Estimated Amount (₹)", "Actual Amount (₹)", "Variance (Over/Under ₹)"]
  ];
  var estBudget = payload.summary ? Number(payload.summary.total_estimated_budget || 0) : 0;
  var actExpense = payload.summary ? Number(payload.summary.total_actual_expenses || 0) : 0;
  var estIncome = payload.summary ? Number(payload.summary.total_estimated_income || 0) : 0;
  var actIncome = payload.summary ? Number(payload.summary.total_actual_income || 0) : 0;
  
  summaryRows.push(["Total Budget / Expenses", estBudget, actExpense, estBudget - actExpense]);
  summaryRows.push(["Total Income / Revenue", estIncome, actIncome, actIncome - estIncome]);
  summaryRows.push(["Net Financial Margin", estIncome - estBudget, actIncome - actExpense, (actIncome - actExpense) - (estIncome - estBudget)]);
  
  summarySheet.getRange(1, 1, summaryRows.length, 4).setValues(summaryRows);
  formatHeaderRow(summarySheet, 4, 4);

  // 2. Income Tab (Estimated vs Actual)
  var incomeSheet = getOrCreateSheet(ss, "💰 Income (Est vs Actual)");
  incomeSheet.clear();
  var incomeHeaders = ["ID", "Type", "Title / Source", "Category", "Target Estimated (₹)", "Actual Received (₹)", "Variance (₹)", "Payment Method", "Status", "Date", "Notes"];
  var incomeRows = [incomeHeaders];
  if (payload.income && payload.income.length > 0) {
    payload.income.forEach(function(row) {
      var est = Number(row.target_amount || (row.type === 'Estimated' ? row.amount : 0) || 0);
      var act = Number(row.actual_amount || (row.type === 'Actual' ? row.amount : 0) || 0);
      incomeRows.push([
        String(row.id || "INC"),
        String(row.type || (act > 0 ? "Actual" : "Estimated")),
        String(row.title || row.source || "Income Source"),
        String(row.category || "General"),
        est,
        act,
        act - est,
        String(row.payment_method || row.payment_mode || "N/A"),
        String(row.status || (act > 0 ? "Received" : "Planned")),
        String(row.date || row.received_on || ""),
        String(row.notes || "")
      ]);
    });
  }
  incomeSheet.getRange(1, 1, incomeRows.length, incomeHeaders.length).setValues(incomeRows);
  formatHeaderRow(incomeSheet, 1, incomeHeaders.length);

  // 3. Expenses Tab (Estimated vs Actual)
  var expenseSheet = getOrCreateSheet(ss, "💸 Expenses (Est vs Actual)");
  expenseSheet.clear();
  var expenseHeaders = ["ID", "Type", "Title / Item", "Department", "Category", "Estimated Budget (₹)", "Actual Spent (₹)", "Variance (₹)", "Receipt URL", "Payment Method", "Date", "Notes"];
  var expenseRows = [expenseHeaders];
  if (payload.expenses && payload.expenses.length > 0) {
    payload.expenses.forEach(function(row) {
      var est = Number(row.estimated_cost || (row.type === 'Estimated' ? row.amount : 0) || 0);
      var act = Number(row.actual_spent || (row.type === 'Actual' ? row.amount : 0) || 0);
      expenseRows.push([
        String(row.id || "EXP"),
        String(row.type || (act > 0 ? "Actual" : "Estimated")),
        String(row.title || row.item_name || "Expense Item"),
        String(row.dept_name || "General"),
        String(row.category || "General"),
        est,
        act,
        est - act,
        String(row.receipt_url || ""),
        String(row.payment_method || row.payment_mode || "N/A"),
        String(row.date || row.paid_on || ""),
        String(row.notes || row.description || "")
      ]);
    });
  }
  expenseSheet.getRange(1, 1, expenseRows.length, expenseHeaders.length).setValues(expenseRows);
  formatHeaderRow(expenseSheet, 1, expenseHeaders.length);

  // 4. Budget Proposals Tab
  var budgetSheet = getOrCreateSheet(ss, "📑 Department Proposals");
  budgetSheet.clear();
  var budgetHeaders = ["ID", "Department", "Proposal Title", "Requested Total (₹)", "Status", "Notes"];
  var budgetRows = [budgetHeaders];
  if (payload.proposals && payload.proposals.length > 0) {
    payload.proposals.forEach(function(row) {
      budgetRows.push([
        String(row.id || "PROP"),
        String(row.dept_name || "General"),
        String(row.title || "Budget Proposal"),
        Number(row.total_amount || 0),
        String(row.status || "Pending"),
        String(row.notes || row.description || "")
      ]);
    });
  }
  budgetSheet.getRange(1, 1, budgetRows.length, budgetHeaders.length).setValues(budgetRows);
  formatHeaderRow(budgetSheet, 1, budgetHeaders.length);

  // 5. Sponsors Tab
  var sponsorSheet = getOrCreateSheet(ss, "🤝 Sponsors");
  sponsorSheet.clear();
  var sponsorHeaders = ["ID", "Sponsor Company", "Tier", "Committed Amount (₹)", "Received Amount (₹)", "Contact Person", "Contact Email", "Status", "Notes"];
  var sponsorRows = [sponsorHeaders];
  if (payload.sponsors && payload.sponsors.length > 0) {
    payload.sponsors.forEach(function(row) {
      var committed = Number(row.committed_amount || row.promised_amount || row.amount || 0);
      var received = Number(row.received_amount || row.amount_received || 0);
      sponsorRows.push([
        String(row.id || "SPN"),
        String(row.name || row.company || "Sponsor"),
        String(row.tier || "General"),
        committed,
        received,
        String(row.contact_name || ""),
        String(row.contact_email || ""),
        String(row.status || "Pledged"),
        String(row.notes || "")
      ]);
    });
  }
  sponsorSheet.getRange(1, 1, sponsorRows.length, sponsorHeaders.length).setValues(sponsorRows);
  formatHeaderRow(sponsorSheet, 1, sponsorHeaders.length);

  // 6. Vendors Tab
  var vendorSheet = getOrCreateSheet(ss, "🏢 Vendors & Quotes");
  vendorSheet.clear();
  var vendorHeaders = ["ID", "Vendor Name", "Category", "Contract / Quote (₹)", "Contact Name", "Contact Email / Phone", "Status", "Notes"];
  var vendorRows = [vendorHeaders];
  if (payload.vendors && payload.vendors.length > 0) {
    payload.vendors.forEach(function(row) {
      var val = Number(row.contract_value || row.quoted_price || row.amount || 0);
      vendorRows.push([
        String(row.id || "VND"),
        String(row.name || "Vendor"),
        String(row.category || "Service"),
        val,
        String(row.contact_name || ""),
        String(row.phone || row.contact_email || ""),
        String(row.status || "Active"),
        String(row.notes || "")
      ]);
    });
  }
  vendorSheet.getRange(1, 1, vendorRows.length, vendorHeaders.length).setValues(vendorRows);
  formatHeaderRow(vendorSheet, 1, vendorHeaders.length);
}

function appendSingleRecord(ss, payload) {
  var sheetName = "📊 Financial Summary";
  if (payload.entity === "income") sheetName = "💰 Income (Est vs Actual)";
  if (payload.entity === "expense") sheetName = "💸 Expenses (Est vs Actual)";
  if (payload.entity === "sponsor") sheetName = "🤝 Sponsors";
  if (payload.entity === "vendor") sheetName = "🏢 Vendors & Quotes";
  if (payload.entity === "proposal") sheetName = "📑 Department Proposals";
  
  var sheet = getOrCreateSheet(ss, sheetName);
  var rec = payload.data || {};
  sheet.appendRow([rec.id || "NEW", rec.title || rec.name || rec.source || rec.item_name || "Record", JSON.stringify(rec), new Date().toLocaleString()]);
}

function getOrCreateSheet(ss, name) {
  var sheet = ss.getSheetByName(name);
  if (!sheet) {
    sheet = ss.insertSheet(name);
  }
  return sheet;
}

function formatHeaderRow(sheet, rowNum, numCols) {
  try {
    var cols = numCols || sheet.getLastColumn() || 1;
    var range = sheet.getRange(rowNum, 1, 1, cols);
    range.setBackground("#1e293b").setFontColor("#ffffff").setFontWeight("bold");
  } catch (err) {}
}
"""

def _dispatch_http_post(url: str, payload: dict, on_success=None):
    try:
        data_bytes = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(
            url,
            data=data_bytes,
            headers={'Content-Type': 'application/json', 'User-Agent': 'EventLedgerAI/2.5'},
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=45) as response:
            res_body = response.read().decode('utf-8')
            print(f"Google Sheets Sync Success: {res_body[:100]}")
            if on_success:
                try:
                    on_success()
                except Exception as cb_err:
                    print(f"on_success callback error: {cb_err}")
    except Exception as err:
        print(f"Google Sheets Sync Error: {err}")

def sync_event_data_to_sheets(conn, event_id: int, action: str, entity: str = "general", data: dict = None):
    """
    Asynchronous non-blocking background daemon thread that dispatches live changes to Google Sheets.
    """
    try:
        cur = execute(conn, "SELECT google_sheets_webhook_url, is_auto_sync_enabled FROM event_integrations WHERE event_id=%s", (event_id,))
        row = cur.fetchone()
        if not row or not row.get("google_sheets_webhook_url") or not row.get("is_auto_sync_enabled"):
            return

        webhook_url = row["google_sheets_webhook_url"].strip()
        if not webhook_url.startswith("http"):
            return

        payload = {
            "action": action,
            "entity": entity,
            "event_id": event_id,
            "data": data or {},
            "timestamp": None
        }

        threading.Thread(target=_dispatch_http_post, args=(webhook_url, payload), daemon=True).start()
    except Exception:
        pass

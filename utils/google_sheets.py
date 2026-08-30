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
  summarySheet.appendRow(["Event Name", payload.event_name || "EventLedger AI"]);
  summarySheet.appendRow(["Last Synced", new Date().toLocaleString()]);
  summarySheet.appendRow([]);
  summarySheet.appendRow(["Metric", "Estimated Amount (₹)", "Actual Amount (₹)", "Variance (Over/Under ₹)"]);
  
  var estBudget = payload.summary ? payload.summary.total_estimated_budget : 0;
  var actExpense = payload.summary ? payload.summary.total_actual_expenses : 0;
  var estIncome = payload.summary ? payload.summary.total_estimated_income : 0;
  var actIncome = payload.summary ? payload.summary.total_actual_income : 0;
  
  summarySheet.appendRow(["Total Budget / Expenses", estBudget, actExpense, estBudget - actExpense]);
  summarySheet.appendRow(["Total Income / Revenue", estIncome, actIncome, actIncome - estIncome]);
  summarySheet.appendRow(["Net Financial Margin", estIncome - estBudget, actIncome - actExpense, (actIncome - actExpense) - (estIncome - estBudget)]);
  formatHeaderRow(summarySheet, 4);

  // 2. Income Tab (Estimated vs Actual)
  var incomeSheet = getOrCreateSheet(ss, "💰 Income (Est vs Actual)");
  incomeSheet.clear();
  incomeSheet.appendRow(["ID", "Title / Source", "Category", "Target Estimated (₹)", "Actual Received (₹)", "Variance (₹)", "Payment Method", "Status", "Date"]);
  if (payload.income && payload.income.length > 0) {
    payload.income.forEach(function(row) {
      var est = Number(row.target_amount || row.amount || 0);
      var act = Number(row.actual_amount || row.amount || 0);
      incomeSheet.appendRow([row.id, row.title || row.source, row.category || "General", est, act, act - est, row.payment_method || "N/A", row.status || "Received", row.date || ""]);
    });
  }
  formatHeaderRow(incomeSheet, 1);

  // 3. Expenses Tab (Estimated vs Actual)
  var expenseSheet = getOrCreateSheet(ss, "💸 Expenses (Est vs Actual)");
  expenseSheet.clear();
  expenseSheet.appendRow(["ID", "Title / Item", "Department", "Estimated Budget (₹)", "Actual Spent (₹)", "Variance (₹)", "Receipt URL", "Payment Method", "Date"]);
  if (payload.expenses && payload.expenses.length > 0) {
    payload.expenses.forEach(function(row) {
      var est = Number(row.estimated_cost || row.amount || 0);
      var act = Number(row.amount || 0);
      expenseSheet.appendRow([row.id, row.title, row.dept_name || "General", est, act, est - act, row.receipt_url || "", row.payment_method || "N/A", row.date || ""]);
    });
  }
  formatHeaderRow(expenseSheet, 1);

  // 4. Budget Proposals Tab
  var budgetSheet = getOrCreateSheet(ss, "📑 Department Proposals");
  budgetSheet.clear();
  budgetSheet.appendRow(["ID", "Department", "Proposal Title", "Requested Total (₹)", "Status", "Description"]);
  if (payload.proposals && payload.proposals.length > 0) {
    payload.proposals.forEach(function(row) {
      budgetSheet.appendRow([row.id, row.dept_name || "General", row.title, row.total_amount || 0, row.status || "Pending", row.description || ""]);
    });
  }
  formatHeaderRow(budgetSheet, 1);

  // 5. Sponsors Tab
  var sponsorSheet = getOrCreateSheet(ss, "🤝 Sponsors");
  sponsorSheet.clear();
  sponsorSheet.appendRow(["ID", "Sponsor Company", "Tier", "Committed Amount (₹)", "Received Amount (₹)", "Contact Person", "Status"]);
  if (payload.sponsors && payload.sponsors.length > 0) {
    payload.sponsors.forEach(function(row) {
      sponsorSheet.appendRow([row.id, row.name || row.company, row.tier || "General", row.committed_amount || 0, row.received_amount || 0, row.contact_name || "", row.status || "Pledged"]);
    });
  }
  formatHeaderRow(sponsorSheet, 1);

  // 6. Vendors Tab
  var vendorSheet = getOrCreateSheet(ss, "🏢 Vendors & Quotes");
  vendorSheet.clear();
  vendorSheet.appendRow(["ID", "Vendor Name", "Category", "Quoted Price (₹)", "Final Paid (₹)", "Contact Phone", "Status"]);
  if (payload.vendors && payload.vendors.length > 0) {
    payload.vendors.forEach(function(row) {
      vendorSheet.appendRow([row.id, row.name, row.category || "Service", row.quoted_price || 0, row.paid_amount || 0, row.phone || "", row.status || "Active"]);
    });
  }
  formatHeaderRow(vendorSheet, 1);
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
  sheet.appendRow([rec.id || "NEW", rec.title || rec.name || "Record", JSON.stringify(rec), new Date().toLocaleString()]);
}

function getOrCreateSheet(ss, name) {
  var sheet = ss.getSheetByName(name);
  if (!sheet) {
    sheet = ss.insertSheet(name);
  }
  return sheet;
}

function formatHeaderRow(sheet, rowNum) {
  try {
    var range = sheet.getRange(rowNum, 1, 1, sheet.getLastColumn());
    range.setBackground("#1e293b").setFontColor("#ffffff").setFontWeight("bold");
  } catch (err) {}
}
"""

def _dispatch_http_post(url: str, payload: dict):
    try:
        data_bytes = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(
            url,
            data=data_bytes,
            headers={'Content-Type': 'application/json', 'User-Agent': 'EventLedgerAI/2.5'},
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=8) as response:
            res_body = response.read().decode('utf-8')
            print(f"Google Sheets Sync Success: {res_body[:100]}")
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
        }

        # Launch in background thread — 0ms blocking call
        threading.Thread(target=_dispatch_http_post, args=(webhook_url, payload), daemon=True).start()
    except Exception as err:
        print(f"Failed to trigger sheets sync: {err}")

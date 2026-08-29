"""
EventLedger AI — FastAPI Backend v2.0
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from routers import auth, events, departments, budget, income, expenses, sponsors, vendors, notifications, users, event_data, reports, activity, vendor_quotes, sponsorship, roi, tasks, reimbursements, leaderboard, certificates

app = FastAPI(
    title="EventLedger AI API",
    description="Event Financial Intelligence — REST API",
    version="2.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(events.router)
app.include_router(event_data.router)
app.include_router(reports.router)
app.include_router(activity.router)
app.include_router(departments.router)
app.include_router(budget.router)
app.include_router(vendor_quotes.router)
app.include_router(income.router)
app.include_router(expenses.router)
app.include_router(sponsors.router)
app.include_router(sponsorship.router)
app.include_router(vendors.router)
app.include_router(notifications.router)
app.include_router(users.router)
app.include_router(roi.router)
app.include_router(tasks.router)
app.include_router(reimbursements.router)
app.include_router(leaderboard.router)
app.include_router(certificates.router)

@app.get("/")
def root():
    return {"app": "EventLedger AI API", "version": "2.0.0", "status": "running"}

@app.get("/health")
def health():
    return {"status": "ok"}

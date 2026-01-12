import sys
import os

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from routers.session import router as session_router
from routers.cash_tally import router_cash_tally

app = FastAPI(
    title="Session and Tally Service API",
    description="Handles cashier sessions and cash tally/close-out procedures.",
    version="1.0.0"
)

# Add CORS BEFORE including routers
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://bleu-pos-tau.vercel.app",
        "https://bleu-ims-beta.vercel.app",
        "https://authservices-npr8.onrender.com",        "http://192.168.100.14:8002",
        "https://bleu-stockservices.onrender.com",
        "https://ims-restockservices.onrender.com",
        "https://blockchainservices.onrender.com",  ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

# Include routers AFTER middleware
app.include_router(session_router, prefix="/api")
app.include_router(router_cash_tally, prefix="/api")

@app.get("/", tags=["Health Check"])
def read_root():
    return {"status": "ok", "message": "Session and Tally Service is running."}

if __name__ == "__main__":
    import uvicorn
    print("--- Starting Session and Tally Service on http://0.0.0.0:9001 ---")
    print("API docs available at http://127.0.0.1:9001/docs")
    uvicorn.run("main:app", port=9001, host="0.0.0.0", reload=True)
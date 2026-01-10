# FILE: cash_tally_router.py (FIXED AND CORRECTED VERSION)

from fastapi import APIRouter, HTTPException, status, Depends, BackgroundTasks
from pydantic import BaseModel
from typing import Dict, Optional
from decimal import Decimal, getcontext
import sys
import os
import httpx
import logging

# Set precision for Decimal calculations
getcontext().prec = 18

# --- Configure logging & DB connection ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from database import get_db_connection

# --- Auth Configuration ---
from fastapi.security import OAuth2PasswordBearer
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="http://127.0.0.1:4000/auth/token")
USER_SERVICE_ME_URL = "http://localhost:4000/auth/users/me"
USER_SERVICE_VERIFY_PIN_URL = "http://localhost:4000/users/verify-pin"
USER_SERVICE_EMPLOYEE_NAME_URL = "http://127.0.0.1:4000/users/employee_name"

# --- Blockchain Configuration ---
BLOCKCHAIN_LOG_URL = "http://localhost:9005/blockchain/log"

# --- Define the new router ---
router_cash_tally = APIRouter(
    prefix="/auth/cash_tally",
    tags=["Cash Tally"]
)

# --- Authorization Helper ---
async def get_current_active_user(token: str = Depends(oauth2_scheme)):
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(USER_SERVICE_ME_URL, headers={"Authorization": f"Bearer {token}"})
            response.raise_for_status()
            user_data = response.json()
            user_data['access_token'] = token
            return user_data
        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=e.response.status_code, detail=f"Invalid token: {e.response.text}", headers={"WWW-Authenticate": "Bearer"})
        except httpx.RequestError:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Auth service unavailable.")

# --- Pydantic Models ---
class CloseSessionRequest(BaseModel):
    sessionId: int
    cashCounts: Dict[str, int]
    pin: str

class ReportDiscrepancyRequest(BaseModel):
    sessionId: int
    discrepancyAmount: float
    reportedBy: str
    pin: str
    cashCounts: Dict[str, int]

# --- Helper function to verify manager PIN ---
async def verify_manager_pin(pin: str, token: str):
    """Verify manager PIN and return manager username"""
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                USER_SERVICE_VERIFY_PIN_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json"
                },
                json={"pin": pin}
            )
            
            if response.status_code == 200:
                data = response.json()
                return data.get("managerUsername")
            else:
                error_detail = response.json().get("detail", "Invalid Manager PIN")
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=error_detail
                )
        except httpx.RequestError:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Auth service unavailable."
            )

# --- Helper function to get employee full name ---
async def get_employee_full_name(username: str, token: str):
    """Fetch the full name of an employee based on username"""
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(
                USER_SERVICE_EMPLOYEE_NAME_URL,
                params={"username": username},
                headers={"Authorization": f"Bearer {token}"}
            )
            
            if response.status_code == 200:
                data = response.json()
                return data.get("fullName", username)
            else:
                logger.warning(f"Could not fetch full name for {username}, using username instead")
                return username
        except httpx.RequestError:
            logger.warning(f"Auth service unavailable when fetching name for {username}, using username instead")
            return username

# --- Blockchain Logging Helper ---
async def log_to_blockchain(
    service_identifier: str,
    action: str,
    entity_type: str,
    entity_id: int,
    actor_username: str,
    change_description: str,
    data: dict,
    token: str
):
    """Log activity to blockchain service (non-blocking)"""
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(
                BLOCKCHAIN_LOG_URL,
                json={
                    "service_identifier": service_identifier,
                    "action": action,
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "actor_username": actor_username,
                    "change_description": change_description,
                    "data": data
                },
                headers={"Authorization": f"Bearer {token}"}
            )
            
            if response.status_code == 201:
                logger.info(f"✅ Blockchain log created: {action} - {entity_type} #{entity_id}")
            else:
                logger.warning(f"⚠️ Blockchain logging failed: {response.status_code} - {response.text}")
                
        except Exception as e:
            logger.error(f"❌ Blockchain logging error: {e}")

# --- Calculate NET cash sales (with refunds deducted) ---
async def calculate_net_cash_sales(cursor, cashier_name: str, session_start_time):
    """
    Calculate net cash sales = Gross Cash Sales - Cash Refunds.
    This version correctly joins tables and calculates net values.
    """
    # Step 1: Calculate GROSS cash sales (net of discounts/promos, but before refunds)
    gross_sales_query = """
        WITH SaleItemNetValue AS (
            SELECT
                -- Calculate the net value of each item line
                (si.UnitPrice * si.Quantity) + ISNULL(addons.TotalAddonPrice, 0) -
                ISNULL(discounts.TotalItemDiscount, 0) - ISNULL(promos.TotalItemPromotion, 0) AS NetLineValue
            FROM Sales s
            JOIN CashierSessions cs ON s.SessionID = cs.SessionID
            JOIN SaleItems si ON s.SaleID = si.SaleID
            LEFT JOIN (
                SELECT sia.SaleItemID, SUM(a.Price * sia.Quantity) AS TotalAddonPrice
                FROM SaleItemAddons sia JOIN Addons a ON sia.AddonID = a.AddonID
                GROUP BY sia.SaleItemID
            ) addons ON si.SaleItemID = addons.SaleItemID
            LEFT JOIN (
                SELECT SaleItemID, SUM(DiscountAmount) AS TotalItemDiscount
                FROM SaleItemDiscounts GROUP BY SaleItemID
            ) discounts ON si.SaleItemID = discounts.SaleItemID
            LEFT JOIN (
                SELECT SaleItemID, SUM(PromotionAmount) AS TotalItemPromotion
                FROM SaleItemPromotions GROUP BY SaleItemID
            ) promos ON si.SaleItemID = promos.SaleItemID
            WHERE s.Status = 'completed'
              AND s.PaymentMethod = 'Cash'
              AND cs.CashierName = ?
              AND s.CreatedAt >= ?
        )
        SELECT ISNULL(SUM(NetLineValue), 0) FROM SaleItemNetValue;
    """
    
    await cursor.execute(gross_sales_query, cashier_name, session_start_time)
    gross_result = await cursor.fetchone()
    gross_cash_sales = Decimal(gross_result[0] if gross_result and gross_result[0] is not None else 0)
    
    # Step 2: Calculate total NET cash refunds during this session
    refunds_query = """
        WITH SaleItemNetValue AS (
            SELECT
                si.SaleItemID,
                si.Quantity AS OriginalQuantity,
                (
                    (si.UnitPrice * si.Quantity) +
                    ISNULL(addons.TotalAddonPrice, 0) -
                    ISNULL(discounts.TotalItemDiscount, 0) -
                    ISNULL(promos.TotalItemPromotion, 0)
                ) AS NetLineValue
            FROM SaleItems si
            LEFT JOIN (
                SELECT sia.SaleItemID, SUM(a.Price * sia.Quantity) AS TotalAddonPrice
                FROM SaleItemAddons sia JOIN Addons a ON sia.AddonID = a.AddonID GROUP BY sia.SaleItemID
            ) addons ON si.SaleItemID = addons.SaleItemID
            LEFT JOIN (
                SELECT SaleItemID, SUM(DiscountAmount) AS TotalItemDiscount
                FROM SaleItemDiscounts GROUP BY SaleItemID
            ) discounts ON si.SaleItemID = discounts.SaleItemID
            LEFT JOIN (
                SELECT SaleItemID, SUM(PromotionAmount) AS TotalItemPromotion
                FROM SaleItemPromotions GROUP BY SaleItemID
            ) promos ON si.SaleItemID = promos.SaleItemID
        )
        SELECT
            ISNULL(SUM(
                CASE
                    WHEN sinv.OriginalQuantity > 0 THEN (sinv.NetLineValue / sinv.OriginalQuantity) * ri.RefundedQuantity
                    ELSE 0
                END
            ), 0) AS TotalNetCashRefunds
        FROM RefundedItems ri
        JOIN RefundedOrders ro ON ri.RefundID = ro.RefundID
        JOIN Sales s ON ro.SaleID = s.SaleID
        JOIN CashierSessions cs ON s.SessionID = cs.SessionID
        JOIN SaleItemNetValue sinv ON ri.SaleItemID = sinv.SaleItemID
        WHERE s.PaymentMethod = 'Cash'
          AND cs.CashierName = ?
          AND ro.RefundedAt >= ?;
    """
    
    await cursor.execute(refunds_query, cashier_name, session_start_time)
    refund_result = await cursor.fetchone()
    total_cash_refunds = Decimal(refund_result[0] if refund_result and refund_result[0] is not None else 0)
    
    net_cash_sales = gross_cash_sales - total_cash_refunds
    
    logger.info(f"=== CASH SALES CALCULATION (Corrected) ===")
    logger.info(f"Cashier: {cashier_name}")
    logger.info(f"Session Start: {session_start_time}")
    logger.info(f"Gross Cash Sales (Net of discounts): ₱{gross_cash_sales}")
    logger.info(f"Total Net Cash Refunds: ₱{total_cash_refunds}")
    logger.info(f"NET Cash Sales (to be expected in drawer): ₱{net_cash_sales}")
    logger.info(f"==========================================")
    
    return net_cash_sales, gross_cash_sales, total_cash_refunds


# --- API Endpoint to Close a Cashier Session (CORRECTED) ---
@router_cash_tally.post(
    "/close_session",
    summary="Close out a cashier's session after a cash count"
)
async def close_session(
    request: CloseSessionRequest,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_active_user),
    token: str = Depends(oauth2_scheme)
):
    allowed_roles = ["admin", "manager", "cashier"]
    if current_user.get("userRole") not in allowed_roles:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied.")

    manager_username = await verify_manager_pin(request.pin, token)
    manager_full_name = await get_employee_full_name(manager_username, token)

    conn = None
    try:
        conn = await get_db_connection()
        async with conn.cursor() as cursor:
            await cursor.execute(
                "SELECT SessionID, CashierName, InitialCash, Status, SessionStart FROM CashierSessions WHERE SessionID = ? AND Status = 'Active'",
                request.sessionId
            )
            session_row = await cursor.fetchone()

            if not session_row:
                raise HTTPException(status_code=404, detail="No active session found with the provided ID.")

            initial_cash = Decimal(session_row.InitialCash)
            cashier_name = session_row.CashierName
            session_start_time = session_row.SessionStart

            net_cash_sales, gross_cash_sales, total_refunds = await calculate_net_cash_sales(
                cursor, cashier_name, session_start_time
            )

            denominations = {
                'bills1000': 1000, 'bills500': 500, 'bills200': 200, 'bills100': 100,
                'bills50': 50, 'bills20': 20, 'coins10': 10, 'coins5': 5, 'coins1': 1,
                'cents25': '0.25', 'cents10': '0.10', 'cents05': '0.05'
            }
            closing_cash = Decimal(0)
            for key, count in request.cashCounts.items():
                if key in denominations:
                    closing_cash += Decimal(denominations[key]) * Decimal(count)

            expected_cash = initial_cash + net_cash_sales
            discrepancy = closing_cash - expected_cash

            TOLERANCE = Decimal('0.01')
            if abs(discrepancy) < TOLERANCE:
                discrepancy = Decimal('0.0')

            update_sql = """
                UPDATE CashierSessions
                SET
                    Status = 'Closed',
                    SessionEnd = GETDATE(),
                    ClosingCash = ?,
                    CashSalesAtClose = ?,
                    CheckedBy = ?
                WHERE SessionID = ?;
            """
            await cursor.execute(
                update_sql,
                float(closing_cash),
                float(gross_cash_sales),
                manager_username,
                request.sessionId
            )
            await conn.commit()

            logger.info(f"✅ Session {request.sessionId} closed successfully")
            logger.info(f"Expected Cash: ₱{expected_cash.quantize(Decimal('0.01'))}")
            logger.info(f"Actual Cash: ₱{closing_cash.quantize(Decimal('0.01'))}")
            logger.info(f"Discrepancy: ₱{discrepancy.quantize(Decimal('0.01'))}")

            blockchain_data = {
                "sessionId": request.sessionId,
                "cashierName": cashier_name,
                "initialCash": float(initial_cash),
                "closingCash": float(closing_cash),
                "grossCashSales": float(gross_cash_sales),
                "cashRefunds": float(total_refunds),
                "netCashSales": float(net_cash_sales),
                "expectedCash": float(expected_cash),
                "discrepancy": float(discrepancy),
                "cashCounts": request.cashCounts,
                "checkedBy": manager_username,
                "verifiedBy": manager_full_name
            }
            
            change_description = (
                f"Session closed. Net sales: ₱{net_cash_sales:.2f}, "
                f"Refunds: ₱{total_refunds:.2f}, Discrepancy: ₱{discrepancy:.2f}"
            )

            background_tasks.add_task(
                log_to_blockchain,
                service_identifier="CASH_TALLY",
                action="CLOSE_SESSION",
                entity_type="CashierSession",
                entity_id=request.sessionId,
                actor_username=cashier_name,
                change_description=change_description,
                data=blockchain_data,
                token=current_user.get('access_token')
            )
            
            return {
                "message": "Session closed successfully",
                "sessionId": request.sessionId,
                "checkedBy": manager_username,
                "verifiedBy": manager_full_name,
                "closingCash": float(closing_cash),
                "grossCashSales": float(gross_cash_sales),
                "cashRefunds": float(total_refunds),
                "netCashSales": float(net_cash_sales),
                "expectedCash": float(expected_cash),
                "discrepancy": float(discrepancy)
            }

    except HTTPException:
        raise
    except Exception as e:
        if conn: await conn.rollback()
        logger.error(f"Failed to close session {request.sessionId}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="An error occurred while closing the session.")
    finally:
        if conn: await conn.close()

# --- API Endpoint to Report Cash Discrepancy (FIXED) ---
@router_cash_tally.post(
    "/report_discrepancy",
    summary="Report a cash discrepancy for a session and close it"
)
async def report_discrepancy(
    request: ReportDiscrepancyRequest,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_active_user),
    token: str = Depends(oauth2_scheme)
):
    allowed_roles = ["admin", "manager", "cashier"]
    if current_user.get("userRole") not in allowed_roles:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied.")

    manager_username = await verify_manager_pin(request.pin, token)
    manager_full_name = await get_employee_full_name(manager_username, token)

    conn = None
    try:
        conn = await get_db_connection()
        async with conn.cursor() as cursor:
            # Get session details
            await cursor.execute(
                "SELECT SessionID, CashierName, InitialCash, Status, SessionStart FROM CashierSessions WHERE SessionID = ? AND Status = 'Active'",
                request.sessionId
            )
            session_row = await cursor.fetchone()

            if not session_row:
                raise HTTPException(status_code=404, detail="No active session found with the provided ID.")

            initial_cash = Decimal(session_row.InitialCash)
            cashier_name = session_row.CashierName
            session_start_time = session_row.SessionStart

            # ✅ FIXED: Calculate NET cash sales
            net_cash_sales, gross_cash_sales, total_refunds = await calculate_net_cash_sales(
                cursor, cashier_name, session_start_time
            )

            # Calculate closing cash
            denominations = {
                'bills1000': 1000, 'bills500': 500, 'bills200': 200, 'bills100': 100,
                'bills50': 50, 'bills20': 20, 'coins10': 10, 'coins5': 5, 'coins1': 1,
                'cents25': 0.25, 'cents10': 0.10, 'cents05': 0.05
            }
            closing_cash = Decimal(0)
            for key, count in request.cashCounts.items():
                if key in denominations:
                    closing_cash += Decimal(denominations[key]) * Decimal(count)

            # Insert discrepancy record
            insert_sql = """
                INSERT INTO CashDiscrepancies 
                (SessionID, DiscrepancyAmount, ReportedBy, ReportedAt, CheckedBy)
                VALUES (?, ?, ?, GETDATE(), ?);
            """
            await cursor.execute(
                insert_sql,
                request.sessionId,
                request.discrepancyAmount,
                request.reportedBy,
                manager_username
            )

            # Close session with GROSS sales
            update_sql = """
                UPDATE CashierSessions
                SET
                    Status = 'Closed',
                    SessionEnd = GETDATE(),
                    ClosingCash = ?,
                    CashSalesAtClose = ?,
                    CheckedBy = ?
                WHERE SessionID = ?;
            """
            await cursor.execute(
                update_sql,
                float(closing_cash),
                float(gross_cash_sales),
                manager_username,
                request.sessionId
            )

            await conn.commit()

            logger.info(f"✅ Discrepancy reported and session {request.sessionId} closed")

            # Blockchain logging
            blockchain_data = {
                "sessionId": request.sessionId,
                "cashierName": cashier_name,
                "initialCash": float(initial_cash),
                "closingCash": float(closing_cash),
                "grossCashSales": float(gross_cash_sales),
                "cashRefunds": float(total_refunds),
                "netCashSales": float(net_cash_sales),
                "discrepancyAmount": request.discrepancyAmount,
                "reportedBy": request.reportedBy,
                "checkedBy": manager_username,
                "verifiedBy": manager_full_name,
                "cashCounts": request.cashCounts
            }
            
            background_tasks.add_task(
                log_to_blockchain,
                service_identifier="CASH_TALLY",
                action="REPORT_DISCREPANCY",
                entity_type="CashDiscrepancy",
                entity_id=request.sessionId,
                actor_username=cashier_name,
                change_description=f"Discrepancy: ₱{request.discrepancyAmount}. Net sales: ₱{float(net_cash_sales)}",
                data=blockchain_data,
                token=current_user.get('access_token')
            )
            
            return {
                "message": "Discrepancy reported and session closed successfully",
                "sessionId": request.sessionId,
                "discrepancyAmount": request.discrepancyAmount,
                "reportedBy": request.reportedBy,
                "checkedBy": manager_username,
                "verifiedBy": manager_full_name,
                "closingCash": float(closing_cash),
                "grossCashSales": float(gross_cash_sales),
                "cashRefunds": float(total_refunds),
                "netCashSales": float(net_cash_sales)
            }

    except HTTPException:
        raise
    except Exception as e:
        if conn:
            await conn.rollback()
        logger.error(f"Failed to report discrepancy for session {request.sessionId}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="An error occurred while reporting the discrepancy.")
    finally:
        if conn:
            await conn.close()